# Bugs found during the refactor (R3: recorded, not fixed)

Each entry: file and line, what, how to reproduce. Nothing here is fixed by the refactor.

## Phase −1 (safety net)

1. **A test patch that intercepts nothing** — `tests/test_base_dir_regression.py:110`
   (`test_use_folder_switches_and_the_action_runs`) patches
   `ophion.gtkui.controllers.group_actions.prompt_text` with `create=True`, but that module has no
   `prompt_text`: the patch creates the attribute for the test and nothing calls it, so the
   `return_value="prod"` it sets is never used. Reproduce:
   `python3 -c "import ophion.gtkui.controllers.group_actions as m; print(hasattr(m, 'prompt_text'))"` →
   `False`. The test still passes because it does not depend on that answer.
   `tests/refactor/test_patch_targets.py` lists it in `CREATED`.

2. **An outer `SSHPASS` wins over the session's password** — `ophion/gtk_terminal.py`,
   `TerminalTab._spawn_command` (≈ L1769–1789). The environment starts from `terminal_env(os.environ)`,
   which removes `TERM`/tmux/screen variables but keeps an `SSHPASS` the app itself was started with; the
   session's password is then **appended** as a second `SSHPASS=…`. A child sees the first entry
   (`getenv`, which `sshpass -e` uses), so every password session would send the outer value. Reproduce:
   start Ophion with `SSHPASS=x` in its environment and connect to a host with a saved password;
   `tests/refactor/golden/spawn_credential_password.json` shows the two entries in this order
   (`SSHPASS=outer-sshpass-value`, then `SSHPASS=<SECRET>`), and
   `GLib.spawn_sync(None, ["/usr/bin/printenv", "SSHPASS"], ["SSHPASS=a", "SSHPASS=b"], …)` prints `a`
   first. (The local shell is not affected: `local_shell_env()` drops `SSHPASS`.)
