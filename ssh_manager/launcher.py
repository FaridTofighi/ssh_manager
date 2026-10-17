"""Terminal launcher utilities for SSH sessions."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import HostEntry
from .ssh_command import build_ssh_command


TERMINAL_BUILDERS: Dict[str, Callable[[str, str, List[str]], List[str]]] = {
    "kitty": lambda program, title, ssh_cmd: [
        program,
        "--title",
        title,
        *ssh_cmd,
    ],
    "wezterm": lambda program, title, ssh_cmd: [
        program,
        "start",
        "--",
        *ssh_cmd,
    ],
    "alacritty": lambda program, title, ssh_cmd: [
        program,
        "-t",
        title,
        "-e",
        *ssh_cmd,
    ],
    "gnome-terminal": lambda program, title, ssh_cmd: [
        program,
        "--title",
        title,
        "--",
        *ssh_cmd,
    ],
    "xfce4-terminal": lambda program, title, ssh_cmd: [
        program,
        "--title",
        title,
        "--command",
        shlex.join(ssh_cmd),
    ],
    "tilix": lambda program, title, ssh_cmd: [
        program,
        "--title",
        title,
        "-e",
        *ssh_cmd,
    ],
    "xterm": lambda program, title, ssh_cmd: [
        program,
        "-T",
        title,
        "-e",
        *ssh_cmd,
    ],
    "konsole": lambda program, title, ssh_cmd: [
        program,
        "--new-tab",
        "-p",
        f"tabtitle={title}",
        "-e",
        *ssh_cmd,
    ],
    "terminator": lambda program, title, ssh_cmd: [
        program,
        "--title",
        title,
        "-e",
        *ssh_cmd,
    ],
}


def _split_program(program: str) -> List[str]:
    try:
        return shlex.split(program)
    except ValueError:
        return [program]


def build_terminal_command(program: str, title: str, ssh_cmd: List[str]) -> Optional[List[str]]:
    program_parts = _split_program(program)
    if not program_parts:
        return None
    executable = program_parts[0]
    extra_args = program_parts[1:]
    program_name = Path(executable).name
    builder = TERMINAL_BUILDERS.get(program_name)
    if builder:
        built = builder(executable, title, ssh_cmd)
        return [built[0], *extra_args, *built[1:]]
    return [*program_parts, "-e", *ssh_cmd]


class ConnectionLauncher:
    """Launch SSH connections through tmux or an available terminal emulator."""

    def __init__(self, terminal_override: Optional[str] = None) -> None:
        self.terminal_override = terminal_override

    def launch(self, host: HostEntry) -> Tuple[bool, str]:
        alias = host.alias
        title = host.display_title()
        ssh_command = build_ssh_command(host)

        if self._try_tmux(ssh_command, title):
            return True, f"Opened {alias} in tmux window"

        command = self._resolve_terminal_command(ssh_command, title)
        if command:
            try:
                subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True, f"Launched {alias} via {command[0]}"
            except OSError as exc:
                return False, f"Failed to run {command[0]}: {exc}"

        return False, (
            f"No suitable terminal found for {alias}. "
            f"Open manually with `{shlex.join(ssh_command)}` or specify --terminal."
        )

    # --------------------------------------------------------------------- impl
    def _try_tmux(self, ssh_command: List[str], title: str) -> bool:
        if "TMUX" not in os.environ:
            return False
        if shutil.which("tmux") is None:
            return False
        window_name = title[:30] or ssh_command[-1][:30]
        command = shlex.join(ssh_command)
        tmux_cmd = ["tmux", "new-window", "-d", "-n", window_name, command]
        try:
            return subprocess.run(tmux_cmd, check=False).returncode == 0
        except OSError:
            return False

    def _resolve_terminal_command(self, ssh_command: List[str], title: str) -> Optional[List[str]]:
        programs: List[str]
        if self.terminal_override:
            programs = [self.terminal_override]
        else:
            programs = [
                prog
                for prog in TERMINAL_BUILDERS
                if shutil.which(prog) is not None
            ]
            env_term = os.environ.get("TERMINAL")
            if env_term and env_term not in programs:
                programs.insert(0, env_term)

        for program in programs:
            program_parts = _split_program(program)
            if not program_parts:
                continue
            executable = program_parts[0]
            resolved = shutil.which(executable)
            if not resolved and program != self.terminal_override:
                continue
            command = build_terminal_command(program, title, ssh_command)
            if command:
                return command
        return None
