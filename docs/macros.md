# Terminal macros

Record what you type in a terminal, give it a name and a shortcut, and replay
it later. Code: `ssh_manager/macros.py` (model, recorder, player, encrypted
store; no GTK), `gtkui/controllers/macro_controller.py` (recording, playback,
menus, shortcuts), `gtkui/dialogs/macros.py` (dialogs).

## Recording

1. Open a connected session (SSH or local terminal) and press **Record macro**
   (the ● button in the session toolbar, *More session actions → Record Macro*,
   or the *Record macro / stop recording* shortcut, which has no default key).
2. Type. The button turns into **Stop (■)** with the elapsed time and the step
   count, the tab gets a red dot, and the tab shows "Recording in <host>.
   Everything you type is saved, including passwords." Switching tabs keeps
   recording that terminal.
3. Press **Stop**. The *Save Macro* dialog asks for a **name** (required,
   unique, at most 80 characters) and a **shortcut** (optional: click the
   button and press the keys; Esc cancels, Backspace removes it), and shows the
   steps. *Keep Recording* goes back to recording, *Discard Recording* throws
   the steps away (it asks first).

Closing the tab or the end of the session stops the recording and offers to
save what was captured. A recording with no steps is not saved.

### What is recorded

What the terminal sends to the program (VTE's `commit` signal), not the
screen. The recorder listens only while it records, to the terminal (pane)
that was active when recording started.

| Step | What it is | Replayed with |
|---|---|---|
| Text | typed text; keystrokes less than 50 ms apart are one step | `feed_child` |
| Paste | pasted text (bracketed-paste markers are removed) | `paste_text`, so the program's bracketed-paste mode is honoured |
| Key | Enter, Tab, Backspace, Escape, Delete, Insert, arrows, Home, End, Page Up / Down, Ctrl+A…Z | the key's bytes (an arrow recorded in application mode replays as recorded) |
| Raw bytes | anything else (F-keys, Alt+key, mouse reports) | the same bytes |
| Pause | the time between steps, kept between 50 ms and 5 s | a timer |

When the recording stops, a Backspace right after typed text removes the last
character of that text (and the pause before the Backspace); cursor keys and
readline editing are kept as they are. Pauses at the start and the end are
dropped. Focus reports (`ESC [ I`, `ESC [ O`) are never recorded.

**Typed or pasted?** A commit within 0.3 s after a key press on the recorded
terminal is typing; longer text without one is a paste.

### Secrets

A text step is **secret** when it was typed while:

* the terminal does not echo in canonical mode (`termios` on the terminal's
  pty: `ECHO` off, `ICANON` on). That is `sudo`, `passwd`, `read -s`,
  `ssh-keygen` and every other password prompt *in a local shell*; or
* the screen ends with a password prompt (`…password:`, `…passphrase:`). `ssh`
  keeps its own pty in raw mode with echo off all the time, so in an SSH tab
  the remote prompt is what tells.

Secret steps are shown as `••••••` everywhere (the save dialog, the step
editor). *Reveal Secrets* in the editor asks for the master password again and
shows them until the dialog closes. *Replace Secret…* types a new one into a
password field. Labels with secrets are never selectable. Steps are never
logged, put into toasts, the message history, host history, settings,
transcripts, temporary files or the clipboard, and macro playback is invisible
to the password-change detection.

## Running a macro

* Its **shortcut**, while a terminal has the keyboard focus. In a text field,
  or anywhere else, a macro's shortcut does nothing, so it can never type into
  a terminal in the background.
* *More session actions → Run Macro ▸* (each with its shortcut hint).
* *Manage Macros… → Run*.

### Which sessions

A macro types into the sessions that typed input would reach, nothing else
(`TerminalNotebook.macro_targets`, which uses the same rule as broadcast
input, `broadcast_target_tabs`):

| Broadcast | The current session is… | The macro runs on |
|---|---|---|
| off | — | the current session |
| on | included | the current session and every other session that is included and connected |
| on | excluded | the current session only (like typing in an excluded session) |

Sessions excluded from the broadcast never receive it, and file browsers and
remote desktops never take part. Local terminals are excluded from the
broadcast by default. In a split tab it is the active pane (the focused one
for a shortcut). The current session must be connected ("Open a connected
session first." otherwise).

* **A snapshot:** the sessions are chosen when the run starts. Turning
  broadcast off, excluding a session or opening a new one affects only the
  next run.
* **Independent:** every session has its own player with the recorded timing;
  a slow session never holds the others up. A session that disconnects or
  closes during the run drops out, and the others go on.
* **Exactly once:** the steps go straight to each terminal, never through the
  broadcast mirroring, so no session gets them twice. Macro input is never
  recorded into another macro and never reaches the password-change detection.
* **Busy sessions:** a session that is already running a macro is skipped
  (and named at the end). If the current session is busy, nothing starts ("A
  macro is already running in this session."). Other sessions can run their
  own macros meanwhile.

**Asking first:** before a run on two or more sessions, Ophion asks "Run
macro “<name>” on N sessions?" with the list of sessions. *Cancel* runs nothing
anywhere. *Don't ask again for multi-session runs* turns the question off; the
checkbox *Ask before running a macro on several sessions* in Manage Macros
turns it back on (`settings.json`: `macros.confirm_multi_session`, default
`true`). A single session is never asked, unless the macro has *Ask before
running it* ("Run macro “<name>” in <host>?"). When both apply, there is only
the multi-session question.

**While it runs** every session shows "Running macro “<name>” (step i/n)".
**Stop** stops that session only; the current session's banner also has
**Stop All**, which stops the run everywhere. At the end of a run on several
sessions one message says "Macro “<name>” finished on N sessions.", or "…
finished on X of N sessions; <sessions> did not complete." with the reason
for each (disconnected, busy, closed, stopped). No macro runs while one is
being recorded. A recording captures only what you type into the recorded
terminal; with broadcast on its banner adds "Broadcast is on: what you type
is also sent to N other sessions."

**Timing:** *As recorded* waits the recorded pauses; *Fast* sends a step every
100 ms and ignores them.

## Managing macros

*Manage Macros…* (session toolbar menu, or the *Manage macros* shortcut): the
list (name, shortcut, steps, last change) with a filter, **Run**, **Edit…**,
**Duplicate** and **Delete…** (it asks, naming the macro; the macro is removed
from the file at once and its shortcut is freed).

**Edit** is the save dialog plus the step editor: move up / down, edit a text
step, a pause or a key, delete, insert text / a key (from a list) / a pause,
replace a secret. Nothing changes until **Save**; **Cancel** leaves the stored
macro as it was.

## Shortcuts and conflicts

A shortcut that is already used is reported while you choose it: "This
shortcut is already assigned to “<action or macro>”." **Replace** takes it
away from the other action or macro when you save; **Choose Another** asks
again. Save stays disabled until the conflict is resolved. A plain key or
Ctrl+letter without Shift/Alt is allowed with a warning: programs in the
terminal would no longer receive it.

Macros' shortcuts are stored inside the encrypted macro file, not in
`settings.json`, and are registered once the file is unlocked. They appear in
the shortcuts window (Ctrl+?) and in Settings → Keyboard Shortcuts, where they
are read-only: a built-in action cannot take a macro's key there; change the
macro first.

## Storage and encryption

`macros.enc` in the configuration folder (`$XDG_CONFIG_HOME/ophion/`,
usually `~/.config/ophion/`):

```json
{"format": "ssh-manager-macros", "version": 2, "keybox_id": "<base64>",
 "cipher": "aes-256-gcm", "nonce": "<base64, 12 bytes>", "data": "<base64>"}
```

* **Everything** (names, shortcuts, descriptions, steps) is inside `data`.
* **Key:** the `macros` subkey of the data key in `keybox.json`, the one key
  the master password protects (see [`docs/security.md`](security.md)).
  There is no key derivation per file: once Ophion is unlocked, the
  macros open at once.
* **Cipher:** AES-256-GCM. The header (`format`, `version`, `keybox_id`,
  `cipher`) is authenticated. A file of another key, a changed header, a
  newer version or a damaged file is refused and kept.
* **Writing:** a temporary file created with mode 0600 in the same folder,
  `fsync`, then rename; the folder is made 0700. The previous file is kept as
  `macros.enc.bak` (also encrypted). Before writing, the file is read again:
  macros another Ophion window saved meanwhile are merged by id (the
  newest change wins; deletions here stay deleted).
* **No drafts:** a recording lives in memory only until you save or discard
  it. There are no temporary files, no autosave and no recovery after a crash.
* **Version 1** (its own key: PBKDF2-HMAC-SHA256 of the master password,
  600 000 iterations, Fernet) is read only by the migration, which rewrites it
  as version 2 and keeps the old file as `macros.enc.pre-keybox` until the
  new one is verified.

`macros.enc` can be copied to another computer together with `keybox.json`;
it is useless without the master password (or the recovery key). **Forgetting
both makes the macros unrecoverable.** Macros are not part of any export or
backup (OpenSSH, MobaXterm, `.rdp`, the inventory backup).

### The master password

* **None set** (*Don't save passwords*): macros cannot be saved, and never in
  plain text. Saving offers **Set Master Password…** (it creates the key
  file) or **Discard Macro**.
* **Changing it** (Settings → Security → *Change Master Password…*) only
  re-wraps the key in `keybox.json`; `macros.enc` is not rewritten.
* **Don't save passwords** is refused while macros exist ("The master
  password stays on: it encrypts your macros…"). Delete the macros first; an
  empty `macros.enc` is removed then.
* **Migration not finished** (safe mode): macros are unavailable until
  Preferences → Security → *Finish Migration…* has run.

### When the file cannot be opened

Wrong key, a changed header, an unsupported version or a damaged / truncated
file: the dialog "The macros file cannot be opened" offers

* **Keep It and Continue Without Macros**: macros are off for this session
  and the file is not touched;
* **Start Empty**: the file is renamed to `macros.enc.damaged-<time>` first.

The file is never overwritten or deleted. If the folder cannot be written, an
error says so and the macro stays in memory; the next change saves it again.

## Limits

200 macros; 5000 steps and 1 MiB of text per macro; names up to 80
characters. Steps of an unknown type (written by a newer version) are kept
but not played.
