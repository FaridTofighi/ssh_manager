"""Create, rename, duplicate and delete parent and child groups."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from ..constants import RowKind  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from ..validation import validate_child_path, validate_group_name  # noqa: E402
from .guards import requires_editable_inventory  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


class GroupActions:
    """Create, rename, duplicate and delete parent and child groups."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win

    @requires_editable_inventory
    def create_parent_group(self) -> None:
        name = self.win._prompt_text(
            _("New Group"),
            _("Name of the new top-level group (a folder under the config directory)."),
            validator=validate_group_name,
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
        if not slug:
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
            validator=validate_group_name, accept_label=_("_Rename"),
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
            validator=validate_group_name, accept_label=_("_Duplicate"),
        )
        if not new_name:
            return
        success, msg = self.win.manager.duplicate_parent_group(source, new_name)
        self.win._log_event(msg, success)
        if success:
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
            _("Rename Child Group"), _("New file name for “{name}”:").format(name=current), current,
            validator=validate_child_path, accept_label=_("_Rename"),
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
            _("Duplicate Child Group"), _("File name of the copy:"), default,
            validator=validate_child_path, accept_label=_("_Duplicate"),
        )
        if not new_name:
            return
        success, msg = self.win.manager.duplicate_child_group(parent, child, new_name)
        self.win._log_event(msg, success)
        if success:
            self.win.tree.rebuild(refresh=True)
            self.win.tree.select_child_group(parent, new_name)

    @requires_editable_inventory
    def delete_parent_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind != RowKind.PARENT.value:
            return
        name = data["parent"]
        if not self.win._confirm(
            _("Delete group “{name}”?").format(name=name),
            detail=_("All child groups and hosts in this folder are deleted. This cannot be undone."),
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
        if not self.win._confirm(
            _("Delete child group “{name}”?").format(name=f"{parent}/{child}"),
            detail=_("The config file and every host in it are deleted. This cannot be undone."),
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
