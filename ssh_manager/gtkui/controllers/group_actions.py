"""Create, rename, duplicate and delete parent and child groups."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from ..constants import RowKind  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from ..validation import validators_for  # noqa: E402
from .guards import requires_editable_inventory  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


class GroupActions:
    """Create, rename, duplicate and delete parent and child groups."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win

    @property
    def _store_names(self) -> bool:
        """App store: readable names, groups are not files (no ``.conf``, no file wording)."""

        return bool(getattr(self.win.manager, "allows_display_names", False))

    def _validator(self, kind: str, *, siblings=None, current: str = ""):
        """Format rules of the backend, plus (app store) an inline duplicate check against ``siblings``."""

        base = validators_for(self.win.manager)[kind]
        if siblings is None or not self._store_names:
            return base
        taken = {name.strip().casefold() for name in siblings if name}
        keep = current.strip().casefold()

        def check(value: str):
            error = base(value)
            if error:
                return error
            name = value.strip().strip("/").casefold()
            if name != keep and name in taken:
                return _("A group with this name already exists here.")
            return None

        return check

    def _top_names(self):
        return list(self.win.manager.tree)

    def _child_siblings(self, parent: str, child: str):
        """Names of the groups next to ``child`` (the full child paths of its holder)."""

        holder = child.rsplit("/", 1)[0] if "/" in child else ""
        prefix = f"{holder}/" if holder else ""
        return [name for name in self.win.manager.tree.get(parent, {})
                if name and name.startswith(prefix) and "/" not in name[len(prefix):]]

    def _host_count(self, parent: str, child: str = "") -> int:
        counter = getattr(self.win.manager, "child_host_count", None)
        if counter is not None:
            return counter(parent, child)
        groups = self.win.manager.tree.get(parent, {})
        if child:
            group = groups.get(child)
            return len(group.hosts) if group else 0
        return sum(len(group.hosts) for group in groups.values())

    def _copy_duplicate_bindings(self) -> None:
        """App store: the hosts of a duplicated group keep their credentials (bound by the new ids)."""

        mapping = getattr(self.win.manager, "last_duplicate_map", None) or {}
        store = self.win._credential_store
        bindings = {new: store.binding_for_host(old) for old, new in mapping.items() if store.binding_for_host(old)}
        if bindings:
            self.win.creds.bind_many(bindings)

    @requires_editable_inventory
    def create_parent_group(self) -> None:
        name = self.win._prompt_text(
            _("New Group"),
            _("Name of the new top-level group.") if self._store_names
            else _("Name of the new top-level group (a folder under the config directory)."),
            validator=self._validator("group", siblings=self._top_names()),
            accept_label=_("_Create"),
        )
        if not name:
            return
        success, msg = self.win.manager.create_parent_group(name)
        self.win._log_event(msg, success)
        if success:
            self.win.tree.rebuild(refresh=True)

    @requires_editable_inventory
    def create_child_group_for_current(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind not in {RowKind.PARENT.value, RowKind.CHILD.value, RowKind.CHILD_FOLDER.value}:
            return
        parent = data.get("parent", "")
        if not parent:
            return
        if self._store_names:
            base_child = data.get("child", "") if kind != RowKind.PARENT.value else ""
            name = self.win._prompt_text(
                _("New Subgroup"),
                _("Name of the new group under {path}:").format(path="/".join(filter(None, (parent, base_child)))),
                validator=self._validator("group", siblings=[name.rsplit("/", 1)[-1] for name in self._child_siblings(
                    parent, f"{base_child}/x" if base_child else "x")]),
                accept_label=_("_Create"),
            )
            if not name:
                return
            child_path = f"{base_child}/{name.strip()}" if base_child else name.strip()
            success, msg = self.win.manager.create_child_group(parent, child_path)
            self.win._log_event(msg, success)
            if success:
                self.win.tree.rebuild(refresh=True)
                self.win.tree.select_child_group(parent, child_path)
            return
        if kind == RowKind.CHILD.value:
            base_child = data.get("child", "")
            default_leaf = base_child.split("/")[-1].rsplit(".", 1)[0] + "-sub.conf"
        elif kind == RowKind.CHILD_FOLDER.value:
            base_child = data.get("child", "")
            default_leaf = base_child.split("/")[-1] + "-sub.conf"
        else:
            base_child = ""
            default_leaf = "new-group.conf"
        path_display = f"{parent}/{base_child.rsplit('.', 1)[0]}" if base_child else parent
        name = self.win._prompt_text(
            "New subgroup",
            f"Filename under {path_display}:",
            default_leaf,
        )
        if not name:
            return
        leaf = self.normalize_group_filename(name)
        if base_child:
            base_prefix = base_child.rsplit(".", 1)[0]
            child_path = f"{base_prefix}/{leaf}" if base_prefix else leaf
        else:
            child_path = leaf
        success, msg = self.win.manager.create_child_group(parent, child_path)
        self.win._log_event(msg, success)
        if success:
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_child_group(parent, child_path)

    @requires_editable_inventory
    def create_child_group(self) -> None:
        parent, _child = self.win.tree.infer_target_group()
        selection = self.win.hosts.pick_group(
            _("New Child Group"),
            allow_new_child=True,
            initial_parent=parent,
            initial_child=None,
            accept_label=_("_Create"),
        )
        if not selection:
            return
        parent_name, child_name, create_child = selection
        child_name = self.normalize_group_filename(child_name)
        if not create_child:
            self.win._notify(_("Enter a new child group name."), Level.INFO)
            return
        success, msg = self.win.manager.create_child_group(parent_name, child_name)
        self.win._log_event(msg, success)
        if success:
            self.win.tree.rebuild(refresh=True)

    def normalize_group_filename(self, name: str) -> str:
        slug = name.strip()
        if not slug or self._store_names:  # app-store groups are not files
            return slug
        return slug if slug.lower().endswith(".conf") else f"{slug}.conf"

    @requires_editable_inventory
    def rename_parent_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind != RowKind.PARENT.value:
            return
        current = data["parent"]
        new_name = self.win._prompt_text(
            _("Rename Group"), _("New name for “{name}”:").format(name=current), current,
            validator=self._validator("group", siblings=self._top_names(), current=current), accept_label=_("_Rename"),
        )
        if not new_name:
            return
        success, msg = self.win.manager.rename_parent_group(current, new_name)
        self.win._log_event(msg, success)
        if success:
            self.win.creds.remap_credential_prefix(f"{current}::", f"{new_name}::")
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_parent_group(new_name)

    @requires_editable_inventory
    def duplicate_parent_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind != RowKind.PARENT.value:
            return
        source = data["parent"]
        default = f"{source}-copy"
        new_name = self.win._prompt_text(
            _("Duplicate Group"), _("Name of the copy:"), default,
            validator=self._validator("group", siblings=self._top_names()), accept_label=_("_Duplicate"),
        )
        if not new_name:
            return
        success, msg = self.win.manager.duplicate_parent_group(source, new_name)
        self.win._log_event(msg, success)
        if success:
            self._copy_duplicate_bindings()
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_parent_group(new_name)

    @requires_editable_inventory
    def rename_child_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind != RowKind.CHILD.value:
            return
        parent = data["parent"]
        current = data["child"]
        new_name = self.win._prompt_text(
            _("Rename Group") if self._store_names else _("Rename Child Group"),
            (_("New name or path for “{name}”:") if self._store_names
             else _("New file name for “{name}”:")).format(name=current), current,
            validator=self._validator("child_path", siblings=self._child_siblings(parent, current), current=current),
            accept_label=_("_Rename"),
        )
        if not new_name:
            return
        success, msg = self.win.manager.rename_child_group(parent, current, new_name)
        self.win._log_event(msg, success)
        if success:
            self.win.creds.remap_credential_prefix(
                f"{parent}::{current}::",
                f"{parent}::{new_name}::",
            )
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_child_group(parent, new_name)

    @requires_editable_inventory
    def duplicate_child_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind != RowKind.CHILD.value:
            return
        parent = data["parent"]
        child = data["child"]
        default = f"{child}-copy"
        new_name = self.win._prompt_text(
            _("Duplicate Group") if self._store_names else _("Duplicate Child Group"),
            _("Name of the copy:") if self._store_names else _("File name of the copy:"), default,
            validator=self._validator("child_path"), accept_label=_("_Duplicate"),
        )
        if not new_name:
            return
        success, msg = self.win.manager.duplicate_child_group(parent, child, new_name)
        self.win._log_event(msg, success)
        if success:
            self._copy_duplicate_bindings()
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_child_group(parent, new_name)

    @requires_editable_inventory
    def delete_parent_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind != RowKind.PARENT.value:
            return
        name = data["parent"]
        count = self._host_count(name)
        if not self.win._confirm(
            _("Delete group “{name}”?").format(name=name),
            detail=ngettext("Its subgroups and {n} host are deleted (saved credentials are kept). "
                            "This cannot be undone.",
                            "Its subgroups and {n} hosts are deleted (saved credentials are kept). "
                            "This cannot be undone.", count).format(n=count),
            confirm_label=_("_Delete Group"),
        ):
            return
        success, msg = self.win.manager.delete_parent_group(name)
        self.win._log_event(msg, success)
        if success:
            self.win.creds.unbind_credential_prefix(f"{name}::")
            self.win.tree.rebuild(refresh=True)

    @requires_editable_inventory
    def delete_child_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind != RowKind.CHILD.value:
            return
        parent = data["parent"]
        child = data["child"]
        count = self._host_count(parent, child)
        if not self.win._confirm(
            _("Delete group “{name}”?").format(name=f"{parent}/{child}") if self._store_names
            else _("Delete child group “{name}”?").format(name=f"{parent}/{child}"),
            detail=ngettext("The group, its subgroups and {n} host are deleted (saved credentials are kept). "
                            "This cannot be undone.",
                            "The group, its subgroups and {n} hosts are deleted (saved credentials are kept). "
                            "This cannot be undone.", count).format(n=count) if self._store_names
            else _("The config file and every host in it are deleted. This cannot be undone."),
            confirm_label=_("_Delete Child Group"),
        ):
            return
        success, msg = self.win.manager.delete_child_group(parent, child)
        self.win._log_event(msg, success)
        if success:
            self.win.creds.unbind_credential_prefix(f"{parent}::{child}::")
            self.win.tree.rebuild(refresh=True)

    def prompt_child_destination(self, parent: str) -> Optional[Tuple[str, bool]]:
        children = sorted(self.win.manager.tree.get(parent, {}).keys(), key=lambda name: name.lower())
        if children:
            listing = ", ".join(children)
            message = f"Child filename under {parent} (existing: {listing})"
            default = children[0]
        else:
            message = f"Create a child filename under {parent}"
            default = ""
        name = self.win._prompt_text("Move to child group", message, default)
        if not name:
            return None
        child = self.normalize_group_filename(name)
        if not child:
            return None
        create_child = child not in children
        return child, create_child
