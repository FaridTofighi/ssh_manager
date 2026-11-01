# Run Command on Hosts

Run one non-interactive command (or a small script) on many hosts at once,
without opening a tab per host, and review the results in a table. Code:
`ophion/multirun/` (engine, report, safety, store; no GTK),
`gtkui/controllers/multirun.py`, `gtkui/dialogs/multirun*.py`.

**Broadcast is different:** broadcast mirrors what you *type* into sessions that
are already open, and you read each terminal yourself. Run Command opens its own
short connections (no tabs) and runs the command once per host. You get one row
per host with the status, exit code, time and output.

## Opening it

- app menu → **Run Command on Hosts…** (the selected hosts), or the
  `multirun.open` shortcut action (no key by default; set one in Settings →
  Keyboard Shortcuts);
- a host's menu (with several hosts selected: all of them), or a group's menu →
  **Run Command on These Hosts…**;
- the filter popover → **Run Command on the Matching Hosts…**: every host the
  search, the filter or a smart group shows.

## The dialog

- **Targets:** a count ("37 hosts selected"; non-SSH hosts are skipped and counted).
  **Change…** opens a chooser with:
  - groups and hosts with check boxes, a search, and **Select All Matches**;
  - a tag filter and *SSH hosts only*.
- **Command:** a script editor (monospace), **Insert Variable**, the **Shell**
  (`sh` by default, `bash`, or any you type), **History** (the last 50) and
  **Saved Commands**.
- **Options:**
  - hosts at a time (10; 1–50);
  - connect timeout (10 s) and command timeout (60 s; 0 = none);
  - output kept per host (1 MiB per stream; beyond it the output is cut and
    marked);
  - stop at the first failure;
  - a terminal (PTY);
  - `LANG=C.UTF-8`.

  Jump hosts, keys and passwords are used as configured for each host.
- **Run / Cancel Run** with a progress bar ("12 / 37 done"). Cancel skips the
  hosts that have not started and stops the running ones. While it runs, the
  command, targets and options are locked; closing asks "Cancel the running
  command?".

### How the command is sent

**By default** the text is written to the stdin of `sh -s` (or the chosen
shell) on the server. Multi-line scripts, quotes and heredocs work without
escaping.

**With a terminal (PTY)** it runs as one `sh -c '…'` string instead; some
programs need a terminal. Then the script is not on stdin.

`LANG=C.UTF-8` is set by the script's first line, because servers rarely
accept environment variables over SSH.

**Variables:** `{host}`, `{alias}`, `{user}` and `{port}` are replaced per host
*before* sending.
- **Verbatim:** the host's values are inserted as they are, with no shell
  quoting. Host names and aliases contain no spaces; quote them yourself if
  yours might.
- **Literal braces:** `{{` and `}}` give `{` and `}`.
- **Other names:** any other `{name}` is left as it is.

## Results

- **The table:** one row per host.
  - **Status, an icon and a word:** Success, Failed (exit n), Timeout, Auth
    failed, Host key changed, Host key unknown, Unreachable, Cancelled, Skipped,
    Running, Queued.
  - **Then:** the host, its group, the exit code, the time and the first line
    of output.
  - **Filters:** All / Failed / Succeeded.
- **The selected host:** its stdout and stderr, with a search, and a notice when
  the output was cut or the login needs typing.
- **Group by Output:** hosts with the same output (trailing whitespace ignored)
  collapse into "Same output on 31 hosts". **Show Differences** gives a unified
  diff between the selected host's output and the majority's.
- **Copy Results:** a tab-separated table, the first line of output per host.
- **Export…:** CSV (host, group, status, exit, duration, stdout, stderr), JSON, or
  plain text per host, chosen by the file's extension.
  - **Formulas:** CSV cells starting with `=`, `+`, `-` or `@` get a leading `'`
    so spreadsheets do not run them.
  - **The only way output reaches the disk:** the file is written 0600; nothing
    else is saved.
- **Re-run Failed Only**, **Re-run All**, **Open Sessions to Failed Hosts**
  (normal terminal tabs; it asks above 10).
- **The app log:** one line per run, with counts and time only, e.g.
  `multirun: 37 hosts, 35 ok, 2 failed, 4.2 s`.

## Safety

- **Asks before running** on more than 5 hosts, and when the command contains a
  known dangerous pattern. The patterns are in one tuple,
  `ophion/multirun/safety.py` `DANGEROUS_PATTERNS`:
  - `rm -rf`, `mkfs`, `dd if=`, `shutdown`, `reboot`, `poweroff`, `halt`,
    `init 0`;
  - a fork bomb, `> /dev/sd…`;
  - `chmod -R`, `chown -R`;
  - `systemctl stop|disable|mask`, `iptables -F`, `kill -9 1`.
- **A dangerous match needs a tick** ("I understand this can disrupt the
  servers") before **Run** is enabled.
- **Advisory only:** the check looks for known patterns and never means a
  command is safe.
- **The question** shows the host count and the hosts in a searchable list.

## Saved commands and history

**Encrypted at rest:** saved commands (name, text, shell, a few options) and the
history (the last 50 commands, text only) are kept in `commands.enc`. They are
encrypted with the master password's key, the same way as macros, because a
command can contain a token.

**Without a master password** ("Don't save passwords") nothing is saved and the
history lasts until the app closes.

**Saved Commands** menu: Save Current Command…, load one, Delete, Clear History.

## Limits

- **Not interactive:** no program that waits for input (editors, `top`, prompts).
  stdin is the script; with a PTY there is no input at all.
- **No sudo password:** `sudo` works only without a password (NOPASSWD). When a
  command fails because sudo wants a password, the result says so; run it in a
  normal session.
- **No login prompts:** a host whose login needs typing (no saved password or key,
  a one-time code) is reported as *Auth failed: needs interactive login* instead
  of opening a dialog per host. Use **Open Sessions to Failed Hosts**.
- **Host keys:** the app's host-key policy applies exactly; a changed or unknown
  key stops that host before anything runs.
