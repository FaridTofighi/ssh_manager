"""Create, edit, duplicate, move, tag and delete hosts."""

from __future__ import annotations

import copy
import dataclasses
import logging
from typing import TYPE_CHECKING, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from ...config import HostEntry  # noqa: E402
from ...credentials import PasswordChoice  # noqa: E402
from ...secure_store import SecureStoreError  # noqa: E402
from ...host_utils import host_option  # noqa: E402
from ...templates import HostTemplate, built_in_templates  # noqa: E402
from ..dialogs.base import is_accept  # noqa: E402
from ..dialogs.bulk_tag import BulkTagDialog  # noqa: E402
from ..dialogs.group_picker import GroupPickerDialog  # noqa: E402
from ..dialogs.host_dialog import HostDialog  # noqa: E402
from ..constants import RowKind  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from ..validation import validators_for  # noqa: E402
from .guards import requires_editable_inventory  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


class HostActions:
    """Create, edit, duplicate, move, tag and delete hosts."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win

    def pick_group(self, title: str, **kwargs) -> Optional[Tuple[str, str, bool]]:
        """Run a :class:`GroupPickerDialog`; return ``(parent, child, create_child)``."""

        picker = GroupPickerDialog(self.win, self.win.manager, title, **kwargs)
        try:
            return picker.selected() if is_accept(picker.run()) else None
        finally:
            picker.destroy()

    def run_host_dialog(self, dialog: HostDialog, parent: str,
                        child: str) -> Tuple[Optional[HostEntry], Optional[str], Optional[PasswordChoice]]:
        """Run ``dialog``; return ``(host, shared credential id, password choice)`` (``host`` None = cancelled)."""

        try:
            if not is_accept(dialog.run()):
                return None, None, None
            return dialog.build_host(parent, child), dialog.selected_credential_id(), dialog.password_choice()
        finally:
            dialog.destroy()

    def host_dialog(self, host: Optional[HostEntry] = None, *, for_copy: bool = False, **kwargs) -> HostDialog:
        """The host dialog; ``for_copy`` (duplicate) starts from the shared credential, never the private one."""

        store = self.win._credential_store
        key = self.win.creds.credential_key(host) if host else ""
        private = store.private_for_host(key) if host and not for_copy else None
        if private is not None:
            selected = private.based_on if store.get(private.based_on or "") else None
        elif host:
            selected = store.shared_for_copy(key)
        else:
            selected = None
        private_has_password: Optional[bool] = None
        if private is not None:
            try:
                private_has_password = store.has_password(private.id)
            except SecureStoreError as exc:  # locked: the dialog says so and keeps the secret
                LOGGER.warning("host password status unavailable: %s", type(exc).__name__)
        return HostDialog(
            self.win,
            host,
            credentials=store.list(),
            selected_credential_id=selected,
            private_credential=private,
            private_has_password=private_has_password,
            password_check=self.win.creds.password_store_problem,
            can_edit=self.win.creds.permissions_for_host(host).get("can_edit", True) if host else True,
            # Only the app-owned inventory stores protocols; folder inventories are OpenSSH config files.
            protocols_enabled=bool(getattr(self.win.manager, "allows_display_names", False)),
            transfer_backend_factory=lambda candidate, cred_id, original=host: self.transfer_backend(
                candidate, cred_id, original),
            metadata=host.metadata if host else {},
            profiles=self.win._profiles,
            alias_validator=validators_for(self.win.manager)["alias"],
            rdp_ssh_hosts=self.tunnel_hosts(exclude=getattr(host, "id", "") if host else ""),
            rdp_engine_preference=self.win._settings.remote_desktop.preferred_engine,
            forwards_host_id=self.win.creds.credential_key(host) if host else "",
            forwards_others=self.win.tunnels.all_forwards() if getattr(self.win, "tunnels", None) else (),
            **kwargs,
        )

    def tunnel_hosts(self, exclude: str = "") -> List[Tuple[str, str]]:
        """``(id, label)`` of the inventory's SSH hosts, for an RDP host's "SSH tunnel" choice (app store only)."""

        store = getattr(self.win.manager, "store", None)
        if store is None:
            return []
        rows = [row for row in store.list_hosts() if (row.protocol or "ssh") == "ssh" and row.id != exclude]
        rows.sort(key=lambda row: row.alias.casefold())
        return [(row.id, f"{row.alias} ({row.hostname})" if row.hostname and row.hostname != row.alias else row.alias)
                for row in rows]

    def transfer_backend(self, host: HostEntry, cred_id: Optional[str], original: Optional[HostEntry] = None):
        """A not-connected backend for the host dialog's "Test connection" (credential or host password)."""

        from ...transfer.factory import make_backend

        store = self.win._credential_store
        user, password = host.option("user") or "", ""
        if cred_id:
            credential = store.get(cred_id)
            user = credential.username if credential and credential.username else user
            password = store.get_password(cred_id) or ""
        elif original is not None:
            password = self.win.creds.resolve_password(original).password or ""
        return make_backend(host, user, password,
                            host_key_policy=getattr(self.win._settings, "host_key_policy", "reject"))

    @requires_editable_inventory
    def prompt_new_host(self, template: Optional[HostTemplate] = None, prefill: Optional[HostEntry] = None) -> None:
        """Add a host; ``prefill`` (an unsaved Quick Connect host) fills the dialog ("Save to Inventory")."""

        parent, child = self.win.tree.infer_target_group()
        create_child = False
        if not parent or not child:
            selection = self.pick_group(
                _("Choose a Group for the New Host"),
                allow_new_child=True,
                initial_parent=parent,
                initial_child=child,
                accept_label=_("_Continue"),
            )
            if not selection:
                return
            parent, child, create_child = selection

        if prefill is not None:
            dialog = self.host_dialog(prefill, for_copy=True)
            dialog.set_title(_("Save to Inventory"))
        else:
            dialog = self.host_dialog(template_defaults=template.defaults if template else None)
        host, cred_id, choice = self.run_host_dialog(dialog, parent, child)
        if not host:
            return
        if create_child:
            ok, msg = self.win.manager.create_child_group(parent, child)
            self.win._log_event(msg, ok)
            if not ok:
                return
        success, msg = self.win.manager.add_host(parent, child, host)
        self.win._log_event(msg, success)
        if success:
            self.win.creds.apply_host_credentials(host, cred_id, choice)
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_host(parent, child, host.alias)

    def save_to_inventory(self, host: HostEntry) -> None:
        """Save an unsaved (Quick Connect) host: the host dialog, filled in, then a group to put it in."""

        hostname = host.option("hostname") or host.alias
        prefill = dataclasses.replace(host, alias=hostname, options=dict(host.options),
                                      metadata=copy.deepcopy(host.metadata), id="")
        self.prompt_new_host(prefill=prefill)

    def new_host_from_template(self, template_id: str) -> None:
        template = next((t for t in built_in_templates() if t.id == template_id), None)
        self.prompt_new_host(template=template)

    @requires_editable_inventory
    def edit_selected(self) -> None:
        if self.win.tree.current_item_is_read_only():
            self.win._notify(_("Editing this host is disabled by local permissions."), Level.INFO)
            return
        hosts = self.win.tree.selected_hosts()
        if len(hosts) > 1:
            self.win._notify(_("Select a single host to edit."), Level.INFO)
            return
        host = self.win.tree.selected_host()
        if not host:
            return
        updated, cred_id, choice = self.run_host_dialog(self.host_dialog(host), host.parent_group, host.child_group)
        if not updated:
            return
        if not self.win.creds.check_secrets_policy(updated):
            return
        changed: List[str] = []
        for key in {"alias", "hostname", "user", "port", "tags"}:
            old_value = host.alias if key == "alias" else (host_option(host, key) or "")
            new_value = updated.alias if key == "alias" else (host_option(updated, key) or "")
            if old_value != new_value and "password" not in key:
                changed.append(key)
        success, msg = self.win.manager.update_host(host.parent_group, host.child_group, host.alias, updated)
        self.win._log_event(msg, success)
        if success:
            old_key, new_key = self.win.creds.credential_key(host), self.win.creds.credential_key(updated)
            if new_key != old_key:  # folder inventory rename: the binding (and a private credential) follows
                self.win.creds.rebind_credential(old_key, new_key)
            self.win.creds.apply_host_credentials(updated, cred_id, choice)
            if changed:
                self.win.sessions.audit_log("edit", updated, extra={"fields": ",".join(changed)})
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_host(updated.parent_group, updated.child_group, updated.alias)

    @requires_editable_inventory
    def duplicate_selected(self) -> None:
        if self.win.tree.current_item_is_read_only():
            self.win._notify(_("Editing this host is disabled by local permissions."), Level.INFO)
            return
        hosts = self.win.tree.selected_hosts()
        if len(hosts) > 1:
            self.win._notify(_("Select a single host to duplicate."), Level.INFO)
            return
        host = self.win.tree.selected_host()
        if not host:
            return
        dialog = self.host_dialog(host, for_copy=True)
        dialog.set_alias(self.win.tree.generate_duplicate_alias(host))
        dialog.set_title(_("Duplicate Host"))
        clone, cred_id, choice = self.run_host_dialog(dialog, host.parent_group, host.child_group)
        if not clone:
            return
        if not self.win.creds.check_secrets_policy(clone):
            return
        success, msg = self.win.manager.add_host(host.parent_group, host.child_group, clone)
        self.win._log_event(msg, success)
        if success:
            self.win.creds.apply_host_credentials(clone, cred_id, choice)
            if self.win.creds.private_credential_for_host(host) and not (choice and choice.host_specific):
                shared = self.win._credential_store.get(cred_id or "")
                self.win._notify(
                    _("“{alias}” uses the credential ‘{name}’; the host-specific password was not copied.").format(
                        alias=clone.alias, name=shared.name) if shared else
                    _("“{alias}” has no saved password; the host-specific password was not copied.").format(alias=clone.alias),
                    Level.INFO,
                )
            self.win.sessions.audit_log("edit", clone, extra={"fields": "duplicate"})
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_host(clone.parent_group, clone.child_group, clone.alias)

    @requires_editable_inventory
    def move_selected(self) -> None:
        if self.win.tree.current_item_is_read_only():
            self.win._notify(_("Editing this host is disabled by local permissions."), Level.INFO)
            return
        hosts = self.win.tree.selected_hosts()
        if len(hosts) > 1:
            self.win._notify(_("Select a single host to move."), Level.INFO)
            return
        host = self.win.tree.selected_host()
        if not host:
            return
        selection = self.pick_group(
            _("Move Host “{alias}”").format(alias=host.alias),
            allow_new_child=True,
            initial_parent=host.parent_group,
            initial_child=host.child_group,
            accept_label=_("_Move"),
        )
        if not selection:
            return
        target_parent, target_child, create_child = selection
        binding = self.win._credential_store.binding_for_host(self.win.creds.credential_key(host))
        success, msg = self.win.manager.move_hosts(
            [(host.parent_group, host.child_group, host.alias)],
            target_parent,
            target_child,
            create_child=create_child,
        )
        self.win._log_event(msg, success)
        if success:
            if binding and not host.id:  # folder inventories key bindings by path; app-store ids are stable
                self.win.creds.bind_credential(
                    f"{target_parent}::{target_child}::{host.alias}",
                    binding,
                )
                self.win.creds.bind_credential(self.win.creds.credential_key(host), None)
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_host(target_parent, target_child, host.alias)

    @requires_editable_inventory
    def delete_selected(self) -> None:
        if self.win.tree.current_item_is_read_only():
            self.win._notify(_("Editing this host is disabled by local permissions."), Level.INFO)
            return
        hosts = self.win.tree.selected_hosts()
        if len(hosts) > 1:
            self.win._notify(_("Select a single item to delete."), Level.INFO)
            return
        host = self.win.tree.selected_host()
        if not host:
            return
        if not self.win._confirm(
            _("Delete host “{alias}”?").format(alias=host.alias),
            detail=_("The host entry is removed from {path}.").format(path=f"{host.parent_group}/{host.child_group}"),
            confirm_label=_("_Delete Host"),
        ):
            return
        success, msg = self.win.manager.delete_host(host.parent_group, host.child_group, host.alias)
        self.win._log_event(msg, success)
        if success:
            self.win.creds.bind_credential(self.win.creds.credential_key(host), None)
            self.win.sessions.audit_log("delete", host)
            self.win.tree.rebuild(refresh=True)

    @requires_editable_inventory
    def bulk_move_selected(self) -> None:
        hosts = self.win.tree.selected_hosts()
        if len(hosts) < 2:
            return
        selection = self.pick_group(_("Move Selected Hosts"), allow_new_child=True, accept_label=_("_Move"))
        if not selection:
            return
        target_parent, target_child, create_child = selection
        key_list = [(h.parent_group, h.child_group, h.alias) for h in hosts]
        ok, msg = self.win.manager.move_hosts(key_list, target_parent, target_child, create_child=create_child)
        self.win._log_event(msg, ok)
        if ok:
            for parent, child, alias in key_list:
                self.win.creds.rebind_credential(
                    f"{parent}::{child}::{alias}",
                    f"{target_parent}::{target_child}::{alias}",
                )
            self.win.tree.rebuild(refresh=True)

    @requires_editable_inventory
    def bulk_tag_selected(self) -> None:
        hosts = self.win.tree.selected_hosts()
        if len(hosts) < 2:
            return
        result = BulkTagDialog(self.win, len(hosts)).ask()
        if result is None:
            return
        tags_to_add, color_val = result
        tag_val = ",".join(tags_to_add)

        if color_val:
            for h in hosts:
                self.win.colors.set_host_color(h.parent_group, h.child_group, h.alias, color_val)

        # Apply tags to each host options (append if necessary) and persist
        if tag_val:
            for h in hosts:
                tags = [t.strip() for t in (host_option(h, "tags") or "").split(",") if t.strip()]
                new_tags = [t for t in tags]
                for part in tag_val.split(","):
                    t = part.strip()
                    if t and t not in new_tags:
                        new_tags.append(t)
                h.options["tags"] = ",".join(new_tags)
                ok, msg = self.win.manager.update_host(h.parent_group, h.child_group, h.alias, h)
                if not ok:
                    self.win._log_event(msg, False)

        self.win.tree.rebuild(refresh=True)
        tree_view = getattr(self.win, "tree_view", None)
        if tree_view:
            tree_view.queue_draw()

    @requires_editable_inventory
    def create_host_in_current_group(self) -> None:
        kind, data = self.win.tree.current_item()
        parent = data.get("parent", "")
        if not parent:
            return
        if kind == RowKind.CHILD.value:
            child = data.get("child", "")
            if not child:
                return
            self.create_host_in_group(parent, child)
        elif kind == RowKind.CHILD_FOLDER.value:
            folder = data.get("child", "") or ""
            name = self.win._prompt_text(
                _("Select subgroup"),
                _("Subgroup under {path}:").format(path=f"{parent}/{folder}"),
                "",
            )
            if not name:
                return
            leaf = self.win.groups.normalize_group_filename(name)
            child = f"{folder}/{leaf}" if folder else leaf
            create_child = child not in self.win.manager.tree.get(parent, {})
            if create_child:
                ok, msg = self.win.manager.create_child_group(parent, child)
                self.win._log_event(msg, ok)
                if not ok:
                    return
            self.create_host_in_group(parent, child)
        elif kind == RowKind.PARENT.value:
            choice = self.win.groups.prompt_child_destination(
                parent, title=_("New Host — choose a group"),
                message=_("Choose the child group of “{parent}” for the new host, or type a new name to create it.")
                .format(parent=parent))
            if not choice:
                return
            child, create_child = choice
            if create_child:
                ok, msg = self.win.manager.create_child_group(parent, child)
                self.win._log_event(msg, ok)
                if not ok:
                    return
            self.create_host_in_group(parent, child)

    @requires_editable_inventory
    def create_host_in_group(self, parent: str, child: str) -> None:
        host, cred_id, choice = self.run_host_dialog(self.host_dialog(), parent, child)
        if not host:
            return
        if not self.win.creds.check_secrets_policy(host):
            return
        success, msg = self.win.manager.add_host(parent, child, host)
        self.win._log_event(msg, success)
        if success:
            self.win.creds.apply_host_credentials(host, cred_id, choice)
            self.win.sessions.audit_log("edit", host, extra={"fields": "new"})
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_host(parent, child, host.alias)

    def edit_current_item(self) -> None:
        kind, _ = self.win.tree.current_item()
        if kind == RowKind.HOST.value:
            self.edit_selected()
        elif kind == RowKind.CHILD.value:
            self.win.groups.rename_child_group()
        elif kind == RowKind.PARENT.value:
            self.win.groups.rename_parent_group()

    def delete_current_item(self) -> None:
        kind, _ = self.win.tree.current_item()
        if kind == RowKind.HOST.value:
            self.delete_selected()
        elif kind == RowKind.CHILD.value:
            self.win.groups.delete_child_group()
        elif kind == RowKind.PARENT.value:
            self.win.groups.delete_parent_group()

    def rename_focused(self) -> None:
        if self.win.tree_view.has_focus():
            self.edit_current_item()
        else:
            self.win.terminal_tabs.rename_current()
