"""A local shell in a session tab ("New tab", like gnome-terminal).

:class:`LocalTerminalTab` is a :class:`TerminalTab` with a different child:
the user's shell (``local_shell.py`` picks it, its folder and its cleaned
environment) instead of ssh. Subclassing keeps everything the workspace
expects from a terminal tab (split panes, tiles, zoom, copy / paste, the mouse
menu, search, transcripts, appearance, pane activation) identical; only the
connection-specific parts are replaced:

* no inventory host, credential, password prompt, sshpass, host key, sentinel,
  password-change tracking or remote statistics;
* the title follows the shell (``Local`` until it sets one);
* when the shell exits the tab closes, or stays with "The shell exited with
  status N" and Restart / Close (``local_shell.on_exit``);
* closing asks only while a job other than the shell runs in the foreground.
"""

from __future__ import annotations

import os
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Gtk, Vte  # type: ignore  # noqa: E402

from ..config import HostEntry  # noqa: E402
from ..gtk_terminal import TerminalTab  # noqa: E402
from ..local_shell import local_shell_env, process_cwd, resolve_shell, shell_argv, start_directory  # noqa: E402
from ..settings import LocalShellSettings  # noqa: E402
from .dialogs.confirm import confirm  # noqa: E402
from .i18n import _  # noqa: E402
from .theme.tokens import Level  # noqa: E402

LOCAL_ALIAS = "Local"
_RESPONSE_RESTART = 1  # the banner's first button (TerminalTab: Reconnect)
_RESPONSE_CLOSE = 2
_RESPONSE_CHOOSE_SHELL = 3


class LocalTerminalTab(TerminalTab):
    """The user's shell in a terminal tab (never an inventory host)."""

    __gtype_name__ = "LocalTerminalTab"
    is_local = True

    def __init__(self, log, font_scale: float = 1.0, *, settings: Optional[LocalShellSettings] = None,
                 cwd: Optional[str] = None, appearance=None) -> None:
        self._local = settings or LocalShellSettings()
        self._start_cwd = cwd
        self._shell_pid: Optional[int] = None
        self._secondary_pid: Optional[int] = None
        self._shell_title = ""
        self._shell_path = ""
        self._last_cwd: Optional[str] = cwd
        self.on_title_changed: Optional[Callable[[], None]] = None  # the notebook refreshes the tab label
        TerminalTab.__init__(self, HostEntry(LOCAL_ALIAS, {"hostname": "localhost"}), log, font_scale,
                             autostart=False, appearance=appearance)
        self._stats_enabled = False  # no remote statistics for a local shell
        self._reconnect_button.set_label(_("_Restart"))
        self._choose_shell_button = self._banner.add_button(_("_Choose Another Shell…"), _RESPONSE_CHOOSE_SHELL)
        self._choose_shell_button.hide()
        self._watch_shell_title(self._terminal)
        self._spawn_local(self._terminal, self._start_cwd)

    # ------------------------------------------------------------------ identity
    @property
    def title(self) -> str:
        return getattr(self, "_custom_title", "") or self._shell_title or _(LOCAL_ALIAS)

    @title.setter
    def title(self, value: str) -> None:
        self._custom_title = (value or "").strip()

    @property
    def shell_path(self) -> str:
        return self._shell_path

    def current_directory(self) -> Optional[str]:
        """The shell's working folder (``/proc``; else what the shell last reported)."""

        return process_cwd(self._shell_pid) or self._last_cwd

    # --------------------------------------------------------------------- shell
    def _spawn_local(self, term: Vte.Terminal, cwd: Optional[str]) -> None:
        shell = resolve_shell(self._local.command)
        self._shell_path = shell
        argv = shell_argv(shell, self._local.login_shell)
        workdir = start_directory("current" if cwd else "home", cwd)
        primary = term is self._terminal
        if primary:
            self._set_status("Connecting…", False)  # the transient state: no banner while the shell starts

        def finished(_term, pid: int, error: Optional[GLib.Error], _data) -> None:
            if error or pid <= 0:
                self._spawn_failed(shell, error.message if error else "")
                return
            if primary:
                self._shell_pid = self._child_pid = pid
                self._last_cwd = workdir
                self._choose_shell_button.hide()
                self._set_status(_("Running"), True)
                self.focus_terminal()
            else:
                self._secondary_pid = pid

        # NO_PARENT_ENVV: VTE otherwise adds envv to the app's own environment, and the variables
        # local_shell_env() removed (SSHPASS, OPHION_* and SSH_MANAGER_*, the AppImage's folders) would come back.
        flags = GLib.SpawnFlags(int(GLib.SpawnFlags.DEFAULT) | int(getattr(Vte, "SPAWN_NO_PARENT_ENVV", 0)))
        term.spawn_async(Vte.PtyFlags.DEFAULT, workdir, argv, local_shell_env(), flags,
                         None, None, -1, None, finished, None)

    def _spawn_failed(self, shell: str, reason: str) -> None:
        self._shell_pid = self._child_pid = None
        self._set_status(_("The shell {shell} could not be started{reason}").format(
            shell=shell, reason=f": {reason}" if reason else "."), False)
        self._choose_shell_button.show()
        self._log(_("Local terminal: {shell} could not be started.").format(shell=shell), Level.WARNING)

    def _spawn(self, override_password=None, *, password_origin: str = "prompt") -> None:  # type: ignore[override]
        self._spawn_local(self._terminal, self.current_directory())

    def _begin_connect(self) -> None:
        self._spawn_local(self._terminal, self.current_directory())

    def reconnect(self) -> None:
        """Restart the shell (in the folder it was in)."""

        if self._session_active:
            return
        self._terminal.reset(True, False)
        self._spawn_local(self._terminal, self.current_directory())

    def _spawn_secondary(self, term: Vte.Terminal) -> None:
        self._watch_shell_title(term)
        self._spawn_local(term, self.current_directory())

    def _on_child_exited(self, _term: Vte.Terminal, status: int) -> None:  # type: ignore[override]
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
        self._child_pid = None
        self._last_cwd = self.current_directory()
        self._shell_pid = None
        policy = self._local.on_exit
        if policy == "close" or (policy == "hold_on_error" and code == 0):
            self._set_status(_("The shell exited"), False)
            self._reveal_banner(False)
            GLib.idle_add(self._request_close)
            return
        self._set_status(_("The shell exited with status {code}").format(code=code), False)

    def _on_banner_response(self, bar: Gtk.InfoBar, response: int) -> None:  # type: ignore[override]
        if response == _RESPONSE_CHOOSE_SHELL:
            if self.on_open_preferences is not None:
                self.on_open_preferences()
            return
        TerminalTab._on_banner_response(self, bar, response)

    def _watch_shell_title(self, term: Vte.Terminal) -> None:
        term.connect("window-title-changed", self._on_shell_title)
        term.connect("current-directory-uri-changed", self._on_shell_directory)

    def _on_shell_title(self, term: Vte.Terminal) -> None:
        if term is not self._terminal:
            return
        title = (term.get_window_title() or "").strip()
        if title and title != self._shell_title:
            self._shell_title = title
            if self.on_title_changed is not None:
                self.on_title_changed()

    def _on_shell_directory(self, term: Vte.Terminal) -> None:
        uri = term.get_current_directory_uri() or ""
        if term is self._terminal and uri.startswith("file://"):
            self._last_cwd = unquote(urlparse(uri).path) or self._last_cwd

    # ------------------------------------------------------------ closing safely
    def foreground_job_running(self) -> bool:
        """A program other than the shell runs in the foreground (``tcgetpgrp`` of the pty)."""

        for term, shell in ((self._terminal, self._shell_pid), (self._secondary_terminal, self._secondary_pid)):
            if term is None or not shell:
                continue
            pty = term.get_pty()
            if pty is None:
                continue
            try:
                if os.tcgetpgrp(pty.get_fd()) not in (shell, os.getpgid(shell)):
                    return True
            except OSError:
                continue
        return False

    def confirm_close(self) -> bool:
        if not self.foreground_job_running():
            return True
        parent = self.get_toplevel()
        return confirm(parent if isinstance(parent, Gtk.Window) else None,
                       _("A process is still running in this terminal. Close anyway?"),
                       _("Closing the tab ends it."), confirm_label=_("_Close Terminal"), destructive=True)

    # -------------------------------- SSH-only behaviour: nothing to do for a local shell
    def _start_password_prompt_poll(self) -> None:
        pass  # never read a local shell's output for password prompts

    def _observe_terminal_input(self, term: Vte.Terminal, text: str) -> None:
        pass  # never track what is typed into a local shell (passwd, sudo …)

    def _stage_detected_password_change(self, password: str, term: Vte.Terminal) -> None:
        pass

    def _persist_detected_password_change(self, password: str) -> None:
        pass

    def _start_stats_updates(self) -> None:
        pass


__all__ = ["LocalTerminalTab", "LOCAL_ALIAS"]
