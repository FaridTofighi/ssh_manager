"""A changed SSH host key: the details, and removing the old key (shared by terminals and Paramiko paths).

Nothing here trusts a key: removing the old line only lets ``ssh`` (or the
file browser) ask again whether to trust the server's key. The line is
removed only when it really belongs to the host (``resolve_changed_entry``),
after a confirmation whose default is *Cancel*, with a backup first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ... import known_hosts as KH  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402
from .confirm import ConfirmDialog, show_message  # noqa: E402

RESPONSE_DETAILS = 1
RESPONSE_REMOVE = 2


def risk_text() -> str:
    return _("The server's host key has changed. This can mean the server was reinstalled — or that someone is "
             "intercepting the connection.")


def verify_text() -> str:
    return _("How to verify: compare the new fingerprint with the one your administrator gives you, ask the "
             "server's owner, or check it from another network. Remove the old key only when you know why "
             "it changed.")


@dataclass
class ChangedKey:
    """What is known about a changed key, from ssh's text or from the Paramiko check."""

    host_label: str
    names: List[Tuple[str, int]]  # the (host name, port) pairs the key belongs to
    new_fingerprint: str
    key_type: str = ""
    known_hosts_path: str = ""
    line_no: int = 0
    old_fingerprints: Tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return Path(self.known_hosts_path).expanduser() if self.known_hosts_path else KH.default_known_hosts()

    def old_entry(self) -> KH.Entry:
        """The stored entry being replaced (verified to belong to the host); raises ``KnownHostsError``."""

        return KH.resolve_changed_entry(KH.KnownHostsFile(self.path), self.line_no, self.names,
                                        self.new_fingerprint)


def changed_key_from_error(error, host_label: str) -> ChangedKey:
    """From ``transfer.backend.HostKeyChanged``."""

    names = [(error.hostname, error.port)] if getattr(error, "hostname", "") else []
    return ChangedKey(host_label=host_label, names=names, new_fingerprint=error.fingerprint,
                      key_type=KH.key_type_label(getattr(error, "key_type", "")),
                      known_hosts_path=str(getattr(error, "known_hosts_path", "") or ""),
                      line_no=int(getattr(error, "line_no", 0) or 0),
                      old_fingerprints=tuple(getattr(error, "old_fingerprints", ()) or
                                             ((error.expected,) if error.expected else ())))


class HostKeyDetailsDialog(BaseDialog):
    def __init__(self, parent: Optional[Gtk.Window], change: ChangedKey, *,
                 open_manager: Optional[Callable[[], None]] = None) -> None:
        BaseDialog.__init__(self, parent, _("Host Key Changed"), size=DialogSize.MEDIUM, resizable=False)
        self.change = change
        intro = Gtk.Label(label=risk_text())
        intro.set_xalign(0.0)
        intro.set_line_wrap(True)
        intro.get_style_context().add_class("section-title")
        self.add_body(intro)
        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        self.rows = {}
        for index, (label, value) in enumerate(self.details()):
            key = Gtk.Label(label=label)
            key.set_xalign(1.0)
            key.set_yalign(0.0)
            key.get_style_context().add_class("detail-key")
            text = Gtk.Label(label=value)
            text.set_xalign(0.0)
            text.set_selectable(True)
            text.set_line_wrap(True)
            text.get_style_context().add_class("mono")
            grid.attach(key, 0, index, 1, 1)
            grid.attach(text, 1, index, 1, 1)
            self.rows[label] = value
        self.add_body(grid)
        how = Gtk.Label(label=verify_text())
        how.set_xalign(0.0)
        how.set_line_wrap(True)
        self.add_body(how)
        if open_manager is not None:
            manager = Gtk.Button.new_with_mnemonic(_("Open _Known Hosts…"))
            manager.set_halign(Gtk.Align.START)
            manager.connect("clicked", lambda *_a: open_manager())
            self.add_body(manager)
        self.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)

    def details(self) -> List[Tuple[str, str]]:
        change = self.change
        rows = [(_("Host"), change.host_label)]
        try:
            entry = change.old_entry()
        except KH.KnownHostsError as exc:
            entry = None
            old = ", ".join(change.old_fingerprints) or str(exc)
        else:
            old = f"{KH.key_type_label(entry.key_type)} {entry.fingerprint}"
        rows.append((_("Stored key"), old))
        rows.append((_("New key"), f"{change.key_type} {change.new_fingerprint}".strip()))
        if entry is not None:
            rows.append((_("Stored in"), f"{change.path}:{entry.line_no}"))
            try:
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(change.path.stat().st_mtime))
                rows.append((_("File last changed"), _("{when} (the file's date; a single line has none)").format(
                    when=stamp)))
            except OSError:
                pass
        return rows


def show_details(parent: Optional[Gtk.Window], change: ChangedKey, *,
                 open_manager: Optional[Callable[[], None]] = None) -> None:
    HostKeyDetailsDialog(parent, change, open_manager=open_manager).run_and_destroy()


def confirm_remove(parent: Optional[Gtk.Window], change: ChangedKey, entry: KH.Entry, action: str) -> bool:
    text = (risk_text() + "\n\n" + _("Removing line {line} of {path} ({type} {fingerprint}) makes ssh ask "
                                     "again whether to trust the server's new key {new}.").format(
        line=entry.line_no, path=change.path, type=KH.key_type_label(entry.key_type), fingerprint=entry.fingerprint,
        new=change.new_fingerprint) + "\n\n" + verify_text())
    return ConfirmDialog(parent, _("Remove the old key of {host}?").format(host=change.host_label), text,
                         confirm_label=action, destructive=True).ask()  # Cancel is the default


def remove_old_key(parent: Optional[Gtk.Window], change: ChangedKey, *, action: str = "",
                   confirm: Optional[Callable[[ChangedKey, KH.Entry], bool]] = None,
                   message: Optional[Callable[[str, str], None]] = None) -> bool:
    """Confirm, back up, remove exactly the old line; ``True`` when it is gone (the caller reconnects)."""

    message = message or (lambda title, text: show_message(parent, title, text))
    try:
        entry = change.old_entry()
    except KH.KnownHostsError as exc:
        message(_("The old key was not removed"), str(exc))
        return False
    ask = confirm or (lambda c, e: confirm_remove(parent, c, e, action or _("_Remove Old Key")))
    if not ask(change, entry):
        return False
    try:
        KH.KnownHostsFile(change.path).remove_entries([entry])
    except (KH.KnownHostsError, OSError) as exc:
        message(_("The old key was not removed"), str(exc))
        return False
    return True


def show_host_key_changed(parent: Optional[Gtk.Window], change: ChangedKey, *,
                          on_retry: Optional[Callable[[], None]] = None,
                          open_manager: Optional[Callable[[], None]] = None) -> str:
    """The Paramiko paths' dialog: Details… / Remove Old Key and Retry… / Close. Returns what was done."""

    while True:
        dialog = BaseDialog(parent, _("Host Key Changed"), size=DialogSize.SMALL, resizable=False)
        label = Gtk.Label(label=risk_text() + "\n\n" + _("New key: {fp}").format(fp=change.new_fingerprint))
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        label.set_selectable(True)
        dialog.add_body(label)
        dialog.add_secondary(_("_Details…"), RESPONSE_DETAILS)
        if on_retry is not None:
            dialog.add_secondary(_("_Remove Old Key and Retry…"), RESPONSE_REMOVE)
        dialog.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        response = dialog.run_and_destroy()
        if response == RESPONSE_DETAILS:
            show_details(parent, change, open_manager=open_manager)
            continue
        if response == RESPONSE_REMOVE and on_retry is not None:
            if remove_old_key(parent, change, action=_("_Remove and Retry")):
                on_retry()
                return "retried"
            continue
        return "closed"


def changed_key_names(hostname: str, port: int, extra: Sequence[Tuple[str, int]] = ()) -> List[Tuple[str, int]]:
    names = [(hostname, int(port or 22))]
    for name, number in extra:
        if name and (name, number) not in names:
            names.append((name, number))
    return names


__all__ = ["ChangedKey", "HostKeyDetailsDialog", "changed_key_from_error", "confirm_remove", "remove_old_key",
           "show_details", "show_host_key_changed", "risk_text", "verify_text", "changed_key_names"]
