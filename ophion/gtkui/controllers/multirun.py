"""Run Command on hosts, the window side: targets from the inventory, the SSH connection, entry points.

The connection is the app's usual one, the same as the file browser, SSH
tunnels and port forwarding: ``transfer.sftp.backend_for(...).open_transport()``
(jump hosts, keys and agent, the saved or credential password, known_hosts
with ``host_key_policy``). There is no second connect path and no dialog per
host: a host whose login needs typing is reported as "Auth failed".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence

from ...multirun.engine import Target
from ..i18n import _, ngettext
from ..theme.tokens import Level

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)


class MultiRunController:
    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.dialog_size: Optional[tuple] = None  # remembered for this session
        self.last_options: Optional[object] = None
        self._store = None
        self._store_key = None

    # ------------------------------------------------- saved commands
    def store(self):
        """Saved commands and history: encrypted with the keybox like macros; memory only without one."""

        from pathlib import Path

        from ...multirun.store import FILE_NAME, CommandFileError, CommandStore

        keybox = getattr(self.win, "keybox", None)
        key_id = keybox.id if keybox is not None and not keybox.locked else None
        if self._store is not None and self._store_key == key_id:
            return self._store
        store = CommandStore(None)
        if key_id is not None:
            candidate = CommandStore(Path(self.win._settings.config_path).parent / FILE_NAME)
            try:
                candidate.unlock(keybox.subkey("commands"), keybox.id)
                store = candidate
            except CommandFileError as exc:
                LOGGER.warning("saved commands cannot be opened (%s)", exc.kind)
                self.win._notify(_("The saved commands cannot be opened; the file is kept unchanged."),
                                 Level.WARNING)
        self._store, self._store_key = store, key_id
        return store

    # -------------------------------------------------------------- hosts
    def all_hosts(self) -> list:
        from ...inventory.openssh import iter_hosts

        return list(iter_hosts(self.win.manager.tree))

    def host_by_key(self) -> Dict[str, object]:
        return {self.win.creds.credential_key(host): host for host in self.all_hosts()}

    def target(self, host) -> Target:
        from ...host_utils import host_connection_params

        resolved = self.win.creds.host_with_resolved_identity(host)
        hostname, port, user = host_connection_params(resolved)
        group = "/".join(part for part in (host.parent_group, host.child_group) if part)
        return Target(key=self.win.creds.credential_key(host), alias=host.alias, hostname=hostname, port=port,
                      user=user, group=group, protocol=getattr(host, "protocol", "ssh") or "ssh")

    def targets(self, keys: Iterable[str]) -> List[Target]:
        hosts = self.host_by_key()
        return [self.target(hosts[key]) for key in keys if key in hosts]

    def connect(self, target: Target, timeout: float):
        """Worker thread: an authenticated transport (the app's usual SSH connection)."""

        from ...transfer.settings import TransferSettings
        from ...transfer.sftp import backend_for

        host = self.host_by_key().get(target.key)
        if host is None:
            raise LookupError("the host no longer exists")
        creds = self.win.creds
        resolved = creds.host_with_resolved_identity(host)
        password = creds.resolve_password(host).password or ""
        settings = TransferSettings.for_host(resolved)[0]
        settings.timeout_connect = timeout
        backend = backend_for(resolved, resolved.option("user") or "", password, settings=settings,
                              host_key_policy=getattr(self.win._settings, "host_key_policy", "reject"),
                              passphrase=self.win.sessions.saved_key_passphrase(resolved))
        return backend.open_transport("Run command")

    # -------------------------------------------------------- entry points
    def open(self, hosts: Sequence = ()) -> None:
        from ..dialogs.multirun import MultiRunDialog

        if getattr(self.win._secure_store, "reason", "") == "migration":
            from .credentials_controller import migration_pending_text

            self.win._notify(migration_pending_text(), Level.WARNING)  # saved passwords cannot be used yet
        keys = [self.win.creds.credential_key(host) for host in hosts]
        MultiRunDialog(self.win, self, keys).present_and_run()

    def open_for_selection(self) -> None:
        self.open(self.win.tree.selected_hosts())

    def open_for_group(self) -> None:
        from ..constants import RowKind

        kind, data = self.win.tree.current_item()
        if kind == RowKind.PARENT.value:
            hosts = self.win.tree.hosts_in_parent_group(data["parent"])
        elif kind in (RowKind.CHILD.value, RowKind.CHILD_FOLDER.value):
            hosts = self.win.tree.hosts_in_child_group(data["parent"], data["child"])
        else:
            hosts = self.win.tree.selected_hosts()
        self.open(hosts)

    def matching_hosts(self) -> list:
        """The hosts the current search / filter (or smart group) shows."""

        return list(self.win.tree.host_lookup.values())

    def open_for_matches(self) -> None:
        self.open(self.matching_hosts())

    def open_sessions(self, keys: Sequence[str]) -> None:
        hosts = self.host_by_key()
        chosen = [hosts[key] for key in keys if key in hosts]
        if not chosen:
            return
        if len(chosen) > 10 and not self.win._confirm(
                ngettext("Open {n} session?", "Open {n} sessions?", len(chosen)).format(n=len(chosen)),
                detail=_("Each opens a terminal tab."), confirm_label=_("_Open"), destructive=False):
            return
        self.win.sessions.open_hosts(chosen, "run command")

    def log(self, line: str) -> None:
        LOGGER.info("%s", line)
        self.win._log_event(line, True)


__all__ = ["MultiRunController"]
