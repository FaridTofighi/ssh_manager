"""Port forwarding in the window: the engine, saving forwards, starting them, status for the UI.

``TunnelController`` owns the :class:`~ophion.tunnels.engine.TunnelManager`
(its callbacks arrive on the GTK main loop), finds hosts by key (the host id;
the path key in folder inventories), saves a host's forwards in its metadata,
and turns engine failures into questions only when the user started the
forward: an unknown host key asks (the same dialog as the file browser), a
login that failed asks for the password (kept in memory for this session),
a changed key shows the host-key dialog. Automatic starts never open dialogs.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib  # type: ignore  # noqa: E402

from ...tunnels import engine as E  # noqa: E402
from ...tunnels.model import Forward, forwards_from_metadata, inventory_forwards, set_forwards  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)
TOAST_INTERVAL_S = 60.0


def _idle(call: Callable[[], None]) -> None:
    def run() -> bool:
        call()
        return False

    GLib.idle_add(run)


class TunnelController:
    def __init__(self, win: "SSHManagerWindow", *, manager: Optional[E.TunnelManager] = None) -> None:
        self.win = win
        self.manager = manager or E.TunnelManager(self._connect, dispatch=_idle)
        self.manager.on_status.append(self._on_status)
        self.manager.on_event.append(self._on_event)
        self.manager.on_failed.append(self._on_failed)
        self._passwords: Dict[str, str] = {}  # host key -> a password typed for tunnels (this session only)
        self._interactive: Set[str] = set()  # hosts the user started now: failures may ask
        self._last_toast: Dict[Tuple[str, str], float] = {}
        self._session_started: Dict[str, Set[str]] = {}  # host -> forwards started with its sessions
        self.listeners: List[Callable[[], None]] = []  # the manager dialog, the status chip
        self.ask_password: Callable[[str], Optional[str]] = self._ask_password
        self.trust_host_key: Callable[[str, object], bool] = self._trust_host_key

    # ------------------------------------------------------------- hosts
    def host_key(self, host) -> str:
        return self.win.creds.credential_key(host)

    def hosts(self) -> list:
        from ...inventory.openssh import iter_hosts

        return [h for h in iter_hosts(self.win.manager.tree) if getattr(h, "protocol", "ssh") in ("ssh", "sftp")]

    def host(self, key: str):
        return next((h for h in self.hosts() if self.host_key(h) == key), None)

    def hosts_with_forwards(self) -> List[Tuple[object, List[Forward]]]:
        found = []
        for host in self.hosts():
            forwards = forwards_from_metadata(host.metadata)
            if forwards:
                found.append((host, forwards))
        return found

    def all_forwards(self) -> List[Tuple[str, Forward]]:
        return inventory_forwards(self.hosts(), self.host_key)

    def save(self, host, forwards: List[Forward]) -> bool:
        """Write the host's forwards (``metadata['forwards']``)."""

        current = self.host(self.host_key(host)) or host
        current.metadata = set_forwards(current.metadata, forwards)
        ok, message = self.win.manager.update_host(current.parent_group, current.child_group, current.alias, current)
        if not ok:
            self.win._notify(_("The forwards were not saved: {error}").format(error=message), Level.ERROR)
        self._changed()
        return ok

    # ------------------------------------------------------------ connect
    def _connect(self, key: str):
        """Worker thread: an authenticated transport for the host (the app's usual SSH connection)."""

        from ...transfer.sftp import backend_for

        host = self.host(key)
        if host is None:
            raise LookupError("the host no longer exists")
        creds = self.win.creds
        resolved = creds.host_with_resolved_identity(host)
        password = self._passwords.get(key)
        if password is None:
            password = creds.resolve_password(host).password or ""
        backend = backend_for(resolved, resolved.option("user") or "", password,
                              host_key_policy=getattr(self.win._settings, "host_key_policy", "reject"),
                              passphrase=self.win.sessions.saved_key_passphrase(resolved))
        return backend.open_transport("Port forwarding")

    # ------------------------------------------------------------- actions
    def start(self, host, forward: Forward, *, interactive: bool = True) -> None:
        if getattr(self.win._secure_store, "reason", "") == "migration":
            from .credentials_controller import migration_pending_text

            self.win._notify(migration_pending_text(), Level.WARNING)
        key = self.host_key(host)
        if interactive:
            self._interactive.add(key)
        self.manager.start(key, forward)

    def stop(self, host, forward: Forward) -> None:
        self.manager.stop(self.host_key(host), forward.id)

    def stop_all(self) -> None:
        self.manager.stop_all()

    def status(self, host, forward: Forward) -> E.TunnelStatus:
        return self.manager.status(self.host_key(host), forward.id)

    def active_count(self) -> int:
        return self.manager.active_count()

    def running_count(self) -> int:
        return self.manager.running_count()

    # ------------------------------------------------- sessions and the app
    def session_opened(self, host) -> int:
        """An interactive SSH tab opened: start its with-session forwards (beside ssh, not as -L)."""

        key = self.host_key(host)
        started = 0
        for forward in forwards_from_metadata(host.metadata):
            if forward.auto_start_with_session and not self.manager.status(key, forward.id).running:
                self.manager.start(key, forward)
                self._session_started.setdefault(key, set()).add(forward.id)
                started += 1
        return started

    def last_session_closed(self, host) -> int:
        """The last tab of the host closed: stop what is set to stop with it."""

        key = self.host_key(host)
        stopped = 0
        for forward in forwards_from_metadata(host.metadata):
            if forward.stop_with_last_session and self.manager.status(key, forward.id).running:
                self.manager.stop(key, forward.id)
                stopped += 1
        self._session_started.pop(key, None)
        return stopped

    def start_with_app(self) -> List[Tuple[str, Forward]]:
        """After the window is shown: one forward at a time (idle), failures summed up in one toast."""

        queue = [(self.host_key(host), forward) for host, forwards in self.hosts_with_forwards()
                 for forward in forwards if forward.auto_start_with_app]
        if not queue:
            return queue
        self._app_failures: List[str] = []
        self._app_pending = {(key, forward.id) for key, forward in queue}
        pending = list(queue)

        def next_one() -> bool:
            if not pending:
                return False
            key, forward = pending.pop(0)
            self.manager.start(key, forward)
            return bool(pending)

        GLib.idle_add(next_one)
        return queue

    # -------------------------------------------------------- engine events
    def _changed(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception as exc:  # a closed dialog must not break the others
                LOGGER.debug("tunnel listener failed (%s)", type(exc).__name__)

    def _on_status(self, key: str, forward_id: str, status: E.TunnelStatus) -> None:
        pending = getattr(self, "_app_pending", set())
        if (key, forward_id) in pending and status.state in (E.ACTIVE, E.FAILED):
            pending.discard((key, forward_id))
            if status.state == E.FAILED:
                host = self.host(key)
                self._app_failures.append(host.alias if host is not None else key)
            if not pending and self._app_failures:
                names = ", ".join(sorted(set(self._app_failures)))
                self.win._notify(ngettext("{n} tunnel did not start ({hosts}). Open Tunnels to retry.",
                                          "{n} tunnels did not start ({hosts}). Open Tunnels to retry.",
                                          len(self._app_failures)).format(n=len(self._app_failures), hosts=names),
                                 Level.WARNING)
        self._changed()

    def _on_event(self, key: str, event: str) -> None:
        now = time.monotonic()
        if now - self._last_toast.get((key, event), -TOAST_INTERVAL_S) < TOAST_INTERVAL_S:
            return  # at most one toast per host and kind a minute
        self._last_toast[(key, event)] = now
        host = self.host(key)
        alias = host.alias if host is not None else key
        if event == "dropped":
            self.win._notify(_("The tunnels of {host} lost their connection; reconnecting…").format(host=alias),
                             Level.WARNING)
        elif event == "recovered":
            self.win._notify(_("The tunnels of {host} are connected again.").format(host=alias), Level.SUCCESS)
        self._changed()

    def _on_failed(self, key: str, kind: str, exc: Optional[BaseException]) -> None:
        interactive = key in self._interactive
        self._interactive.discard(key)
        host = self.host(key)
        if host is None or not interactive:
            self._changed()
            return
        forwards = [f for f in forwards_from_metadata(host.metadata)
                    if self.manager.status(key, f.id).state == E.FAILED and
                    self.manager.status(key, f.id).error_kind == kind]
        if kind == "unknown_host_key" and exc is not None and self.trust_host_key(host.alias, exc):
            for forward in forwards:
                self.start(host, forward)
        elif kind == "auth":
            password = self.ask_password(host.alias)
            if password:
                self._passwords[key] = password
                for forward in forwards:
                    self.start(host, forward)
        elif kind == "host_key_changed" and exc is not None:
            from ..dialogs.host_key import changed_key_from_error, show_host_key_changed

            dialogs = getattr(self.win, "app_dialogs", None)
            show_host_key_changed(self.win, changed_key_from_error(exc, host.alias),
                                  on_retry=lambda: [self.start(host, f) for f in forwards],
                                  open_manager=(lambda: dialogs.open_known_hosts(host)) if dialogs else None)
        self._changed()

    # ------------------------------------------------------------- dialogs
    def _ask_password(self, alias: str) -> Optional[str]:
        from ..dialogs.secrets import UnlockDialog

        return UnlockDialog(self.win, _("Password for {host}").format(host=alias),
                            _("The tunnels of {host} need a password").format(host=alias),
                            _("It is used for the tunnels of this session only and not saved."),
                            lambda text: bool(text), accept_label=_("_Connect")).ask()

    def _trust_host_key(self, alias: str, exc) -> bool:
        from ...transfer.sftp import SftpBackend
        from ..dialogs.trust import trust_host_key

        if not trust_host_key(self.win, alias, exc.key_type, exc.fingerprint,
                              other_key_types=getattr(exc, "other_key_types", ()),
                              other_fingerprints=getattr(exc, "other_fingerprints", ())):
            return False
        try:
            SftpBackend(exc.hostname, exc.port).accept_host_key(exc)
        except OSError as error:
            self.win._notify(_("Could not update known_hosts: {error}").format(error=error), Level.WARNING)
            return False
        return True

    # --------------------------------------------------------------- quit
    def confirm_quit(self, confirm: Optional[Callable[[str, str], bool]] = None) -> bool:
        """``True`` when the window may close (tunnels stopped or none running)."""

        count = self.running_count()
        if not count:
            return True
        from ..dialogs.confirm import confirm as ask

        question = ngettext("{n} tunnel is running. Stop it and quit?", "{n} tunnels are running. Stop them and quit?",
                            count).format(n=count)
        ok = (confirm or (lambda title, text: ask(self.win, title, text, confirm_label=_("Stop and _Quit"))))(
            question, _("Programs using the forwarded ports lose their connections."))
        if ok:
            self.stop_all()
        return ok

    def shutdown(self) -> None:
        self.manager.stop_all(wait=1.0)


__all__ = ["TunnelController"]
