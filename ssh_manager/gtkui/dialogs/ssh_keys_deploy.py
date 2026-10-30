"""Install SSH Key on hosts ("ssh-copy-id"): choose the key and the hosts, confirm, results per host.

Each host is reached with its usual connection and saved credentials (the same
path as Run Command and the file browser, host-key policy included), the key
is appended to ``~/.ssh/authorized_keys`` only when it is not there, then a
second connection with **only this key** checks that it works. After a run,
"Set as the key for these hosts" (off by default) writes ``IdentityFile`` for
the hosts where it worked. Nothing here turns password login off.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ... import ssh_keys as K  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level, Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402
from .confirm import confirm  # noqa: E402

COL_KEY, COL_ALIAS, COL_STATUS, COL_MESSAGE, COL_ICON = range(5)
ICONS = {K.INSTALLED: "emblem-ok-symbolic", K.ALREADY_PRESENT: "emblem-ok-symbolic",
         K.AUTH_FAILED: "dialog-password-symbolic", K.HOST_KEY_CHANGED: "dialog-warning-symbolic",
         K.HOST_KEY_UNKNOWN: "dialog-question-symbolic", K.UNREACHABLE: "network-offline-symbolic",
         K.TIMEOUT: "alarm-symbolic", K.FAILED: "dialog-error-symbolic", K.QUEUED: "content-loading-symbolic",
         K.RUNNING: "media-playback-start-symbolic", K.CANCELLED: "process-stop-symbolic"}


class DeployKeyDialog(BaseDialog):
    def __init__(self, parent: Optional[Gtk.Window], win, key: Optional[K.KeyInfo] = None,
                 host_keys: Sequence[str] = ()) -> None:
        BaseDialog.__init__(self, parent, _("Install SSH Key on Hosts"), size=DialogSize.LARGE, height=520)
        self.win = win
        self.keys = [info for info in win.ssh_keys.keys() if info.public_line]
        self.host_keys: List[str] = list(dict.fromkeys(host_keys))
        self.job: Optional[K.DeployRun] = None
        self._poll_id = 0
        self._iters: Dict[str, Gtk.TreeIter] = {}
        # Test hooks.
        self.confirm_run: Callable[[str, int], bool] = lambda title, count: confirm(
            self, title, _("Each host is reached with its saved credentials; the key is added only if it is "
                           "not there yet."), confirm_label=_("_Install"), default_confirm=True)
        self.choose_hosts: Callable[[], Optional[set]] = self._choose_hosts

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        top.pack_start(Gtk.Label(label=_("Key")), False, False, 0)
        self.key_combo = Gtk.ComboBoxText()
        for info in self.keys:
            self.key_combo.append(str(info.path), f"{info.name} — {info.type_label} — {info.fingerprint}")
        if key is not None and key.public_line:
            self.key_combo.set_active_id(str(key.path))
        elif self.keys:
            self.key_combo.set_active(0)
        set_accessible_name(self.key_combo, _("The key to install"))
        self.key_combo.connect("changed", lambda *_a: self._sync())
        top.pack_start(self.key_combo, True, True, 0)
        self.add_body(top)
        hosts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.hosts_label = Gtk.Label()
        self.hosts_label.set_xalign(0.0)
        hosts.pack_start(self.hosts_label, True, True, 0)
        self.change = Gtk.Button.new_with_mnemonic(_("_Choose Hosts…"))
        self.change.connect("clicked", lambda *_a: self.change_hosts())
        hosts.pack_end(self.change, False, False, 0)
        self.add_body(hosts)

        self.store = Gtk.ListStore(str, str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        set_accessible_name(self.view, _("Results per host"))
        status = Gtk.TreeViewColumn(_("Status"))
        icon = Gtk.CellRendererPixbuf()
        status.pack_start(icon, False)
        status.add_attribute(icon, "icon-name", COL_ICON)
        word = Gtk.CellRendererText()
        status.pack_start(word, True)
        status.add_attribute(word, "text", COL_STATUS)
        self.view.append_column(Gtk.TreeViewColumn(_("Host"), Gtk.CellRendererText(), text=COL_ALIAS))
        self.view.append_column(status)
        message = Gtk.CellRendererText()
        message.set_property("ellipsize", Pango.EllipsizeMode.END)
        column = Gtk.TreeViewColumn(_("Message"), message, text=COL_MESSAGE)
        column.set_expand(True)
        self.view.append_column(column)
        self.view.set_tooltip_column(COL_MESSAGE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.view)
        self.add_body(scroller, expand=True)
        self.set_identity = Gtk.CheckButton.new_with_mnemonic(
            _("_Set as the key for the hosts where it works (IdentityFile)"))
        self.add_body(self.set_identity)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.install_button = Gtk.Button.new_with_mnemonic(_("_Install"))
        self.install_button.get_style_context().add_class("suggested-action")
        self.install_button.connect("clicked", lambda *_a: self.start())
        self.retry_button = Gtk.Button.new_with_mnemonic(_("_Retry Failed"))
        self.retry_button.connect("clicked", lambda *_a: self.retry_failed())
        self.copy_button = Gtk.Button.new_with_mnemonic(_("_Copy Report"))
        self.copy_button.connect("clicked", lambda *_a: self.copy_report())
        self.apply_button = Gtk.Button.new_with_mnemonic(_("_Apply Key Setting"))
        self.apply_button.connect("clicked", lambda *_a: self.apply_identity())
        for button in (self.install_button, self.retry_button, self.copy_button, self.apply_button):
            actions.pack_start(button, False, False, 0)
        self.add_body(actions)
        self.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        self.connect("destroy", lambda *_a: self._stop_poll())
        self.set_identity.connect("toggled", lambda *_a: self._sync())
        self._update_hosts()

    # ------------------------------------------------------------ hosts
    def _hosts(self) -> Dict[str, object]:
        return self.win.multirun.host_by_key()

    def _update_hosts(self) -> None:
        count = len([key for key in self.host_keys if key in self._hosts()])
        self.hosts_label.set_text(ngettext("{n} host", "{n} hosts", count).format(n=count))
        self._sync()

    def _choose_hosts(self) -> Optional[set]:
        from .multirun_targets import TargetChooserDialog

        return TargetChooserDialog(self, self.win.manager.tree, self.host_keys,
                                   key=self.win.creds.credential_key).ask()

    def change_hosts(self) -> None:
        chosen = self.choose_hosts()
        if chosen is not None:
            order = {key: index for index, key in enumerate(self._hosts())}
            self.host_keys = sorted(chosen, key=lambda key: order.get(key, len(order)))
            self._update_hosts()

    def selected_key(self) -> Optional[K.KeyInfo]:
        active = self.key_combo.get_active_id()
        return next((info for info in self.keys if str(info.path) == active), None)

    @property
    def running(self) -> bool:
        return self.job is not None and not self.job.done

    def _sync(self) -> None:
        running = self.running
        ready = self.selected_key() is not None and bool(self.host_keys) and not running
        self.install_button.set_sensitive(ready)
        self.change.set_sensitive(not running)
        self.key_combo.set_sensitive(not running)
        finished = self.job is not None and not running
        results = self.job.results() if finished else []
        self.retry_button.set_sensitive(any(r.status in K.DEPLOY_FAILURES for r in results))
        self.copy_button.set_sensitive(finished)
        works = [r for r in results if r.verified]
        self.apply_button.set_sensitive(bool(works) and self.set_identity.get_active())

    # -------------------------------------------------------------- run
    def _verify(self, key: K.KeyInfo):
        def verify(target, timeout):
            """A connection with only this key: no agent, no password (the host's jump host still applies)."""

            from ...transfer.settings import TransferSettings
            from ...transfer.sftp import backend_for

            host = self._hosts()[target.key]
            resolved = self.win.creds.host_with_resolved_identity(host)
            settings = TransferSettings.for_host(resolved)[0]
            settings.timeout_connect = timeout
            backend = backend_for(resolved, resolved.option("user") or "", "", settings=settings,
                                  host_key_policy=getattr(self.win._settings, "host_key_policy", "reject"),
                                  passphrase=self.win.ssh_keys.saved_passphrase(key.path) or "")
            backend.key_file = str(key.path)
            backend.allow_agent = False
            return backend.open_transport("Key check")

        return verify

    def start(self, keys: Optional[Sequence[str]] = None) -> Optional[K.DeployRun]:
        key = self.selected_key()
        chosen = list(keys if keys is not None else self.host_keys)
        if key is None or not chosen or self.running:
            return None
        title = ngettext("Install key {key} on {n} host using its saved credentials?",
                         "Install key {key} on {n} hosts using their saved credentials?",
                         len(chosen)).format(key=key.name, n=len(chosen))
        if not self.confirm_run(title, len(chosen)):
            return None
        targets = self.win.multirun.targets(chosen)
        self.store.clear()
        self._iters = {t.key: self.store.append([t.key, t.alias, K.deploy_status_label(K.QUEUED), "",
                                                 ICONS[K.QUEUED]]) for t in targets}
        self.job = K.DeployRun(targets, key.public_line, connect=self.win.multirun.connect,
                               verify=self._verify(key))
        self.job.start()
        self._stop_poll()
        self._poll_id = GLib.timeout_add(150, self._poll)
        self._sync()
        return self.job

    def _poll(self) -> bool:
        run = self.job
        if run is None:
            self._poll_id = 0
            return False
        for result in run.results():
            tree_iter = self._iters.get(result.key)
            if tree_iter is None:
                continue
            values = (K.deploy_status_label(result.status), result.message, ICONS.get(result.status, ""))
            for column, value in zip((COL_STATUS, COL_MESSAGE, COL_ICON), values):
                if self.store[tree_iter][column] != value:
                    self.store[tree_iter][column] = value
        if run.done:
            self._poll_id = 0
            hosts, ok = len(run.results()), sum(r.status in (K.INSTALLED, K.ALREADY_PRESENT) for r in run.results())
            self.win._log_event(f"ssh keys: installed on {ok} of {hosts} hosts", True)
            self._sync()
            return False
        return True

    def _stop_poll(self) -> None:
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0

    def retry_failed(self) -> Optional[K.DeployRun]:
        if self.job is None:
            return None
        failed = [r.key for r in self.job.results() if r.status in K.DEPLOY_FAILURES]
        return self.start(failed) if failed else None

    def report(self) -> str:
        lines = ["host\tstatus\tmessage"]
        for result in self.job.results() if self.job else []:
            lines.append("\t".join((result.alias, K.deploy_status_label(result.status),
                                    result.message.replace("\t", " ").replace("\n", " "))))
        return "\n".join(lines) + "\n"

    def copy_report(self) -> str:
        text = self.report()
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        return text

    def apply_identity(self) -> int:
        """IdentityFile = this key for the hosts where the key login works (one transaction, with Undo)."""

        key = self.selected_key()
        if key is None or self.job is None or not self.set_identity.get_active():
            return 0
        hosts = self._hosts()
        works = [r.key for r in self.job.results() if r.verified]
        manager = self.win.manager
        previous = {}
        transaction = getattr(manager, "transaction", None)
        from contextlib import nullcontext

        with (transaction() if callable(transaction) else nullcontext()):
            for host_key in works:
                host = hosts.get(host_key)
                if host is None:
                    continue
                previous[host_key] = host.option("identityfile") or ""
                for name in [k for k in host.options if k.lower() == "identityfile"]:
                    del host.options[name]
                host.options["IdentityFile"] = str(key.path)
                manager.update_host(host.parent_group, host.child_group, host.alias, host)
        self.win.tree.rebuild(refresh=False)
        count = len(previous)

        def undo() -> None:
            current = self.win.multirun.host_by_key()
            for host_key, old in previous.items():
                host = current.get(host_key)
                if host is None:
                    continue
                for name in [k for k in host.options if k.lower() == "identityfile"]:
                    del host.options[name]
                if old:
                    host.options["IdentityFile"] = old
                manager.update_host(host.parent_group, host.child_group, host.alias, host)
            self.win.tree.rebuild(refresh=False)

        self.undo = undo
        self.win._notify(ngettext("{n} host uses the key now.", "{n} hosts use the key now.", count).format(n=count),
                         Level.SUCCESS, action_label=_("Undo"), action=undo)
        return count

    def ask(self) -> None:
        self.run_and_destroy()


__all__ = ["DeployKeyDialog"]
