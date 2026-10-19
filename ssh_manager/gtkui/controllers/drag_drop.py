"""Drag-and-drop moves of hosts and child groups in the tree."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402

from ..constants import RowKind, TreeColumn  # noqa: E402
from ..i18n import _  # noqa: E402
from .guards import requires_editable_inventory  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)

DND_TARGET = "ssh-manager/tree-row"


class DragDropController:
    """Drag-and-drop moves of hosts and child groups in the tree."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.drag_dest_info = None
        self.targets: list = []

    def configure(self) -> None:
        self.drag_dest_info: Optional[Tuple[Gtk.TreePath, Gtk.TreeViewDropPosition]] = None
        self.targets = [Gtk.TargetEntry.new(DND_TARGET, Gtk.TargetFlags.SAME_APP, 0)]
        self.win.tree_view.enable_model_drag_source(
            Gdk.ModifierType.BUTTON1_MASK, self.targets, Gdk.DragAction.MOVE
        )
        self.win.tree_view.enable_model_drag_dest(self.targets, Gdk.DragAction.MOVE)
        self.win.tree_view.connect("drag-data-get", self.on_drag_data_get)
        self.win.tree_view.connect("drag-data-received", self.on_drag_data_received)
        self.win.tree_view.connect("drag-motion", self.on_drag_motion)
        self.win.tree_view.connect("drag-drop", self.on_drag_drop)

    def on_drag_motion(
        self,
        widget: Gtk.TreeView,
        context: Gdk.DragContext,
        x: int,
        y: int,
        time: int,
    ) -> bool:
        target = widget.get_dest_row_at_pos(x, y)
        if target:
            path, position = target
            widget.set_drag_dest_row(path, position)
            self.drag_dest_info = (path, position)
        else:
            self.drag_dest_info = None
        context.drag_status(Gdk.DragAction.MOVE, time)
        return True

    def on_drag_drop(
        self,
        widget: Gtk.TreeView,
        context: Gdk.DragContext,
        x: int,
        y: int,
        time: int,
    ) -> bool:
        target = widget.get_dest_row_at_pos(x, y)
        if target:
            self.drag_dest_info = target
        atom_list = context.list_targets()
        if not atom_list:
            return False
        widget.drag_get_data(context, atom_list[0], time)
        return True

    def on_drag_data_get(
        self,
        _widget: Gtk.TreeView,
        _context: Gdk.DragContext,
        selection: Gtk.SelectionData,
        _info: int,
        _time: int,
    ) -> None:
        payload = self.serialize_payload()
        if not payload:
            selection.set(selection.get_target(), 8, b"")
            return
        data = json.dumps(payload).encode("utf-8")
        selection.set(selection.get_target(), 8, data)

    def serialize_payload(self) -> Optional[Dict[str, object]]:
        model, paths = self.win.tree_view.get_selection().get_selected_rows()
        if not model or not paths:
            return None
        items: List[Dict[str, str]] = []
        payload_kind: Optional[str] = None
        for path in paths:
            tree_iter = model.get_iter(path)
            if not tree_iter:
                continue
            kind = model.get_value(tree_iter, TreeColumn.KIND)
            if kind not in {RowKind.HOST.value, RowKind.CHILD.value}:
                return None
            if payload_kind and kind != payload_kind:
                return None
            payload_kind = kind
            if kind == RowKind.HOST.value:
                items.append(
                    {
                        "kind": "host",
                        "parent": model.get_value(tree_iter, TreeColumn.PARENT),
                        "child": model.get_value(tree_iter, TreeColumn.CHILD),
                        "alias": model.get_value(tree_iter, TreeColumn.ALIAS),
                    }
                )
            elif kind == RowKind.CHILD.value:
                items.append(
                    {
                        "kind": "child",
                        "parent": model.get_value(tree_iter, TreeColumn.PARENT),
                        "child": model.get_value(tree_iter, TreeColumn.CHILD),
                    }
                )
        if not items or not payload_kind:
            return None
        return {"kind": payload_kind, "items": items}

    def resolve_drop_target(
        self, path: Gtk.TreePath, position: Gtk.TreeViewDropPosition
    ) -> Optional[Dict[str, str]]:
        tree_iter = self.win.tree_store.get_iter(path)
        if not tree_iter:
            return None
        kind = self.win.tree_store.get_value(tree_iter, TreeColumn.KIND)
        if kind == RowKind.HOST.value:
            return {
                "type": "child",
                "parent": self.win.tree_store.get_value(tree_iter, TreeColumn.PARENT),
                "child": self.win.tree_store.get_value(tree_iter, TreeColumn.CHILD),
            }
        if kind == RowKind.CHILD.value:
            return {
                "type": "child",
                "parent": self.win.tree_store.get_value(tree_iter, TreeColumn.PARENT),
                "child": self.win.tree_store.get_value(tree_iter, TreeColumn.CHILD),
            }
        if kind == RowKind.PARENT.value:
            return {
                "type": "parent",
                "parent": self.win.tree_store.get_value(tree_iter, TreeColumn.PARENT),
            }
        return None

    def on_drag_data_received(
        self,
        widget: Gtk.TreeView,
        context: Gdk.DragContext,
        _x: int,
        _y: int,
        selection: Gtk.SelectionData,
        _info: int,
        time: int,
    ) -> None:
        data = selection.get_data()
        success = False
        if data:
            try:
                payload = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict) and self.drag_dest_info:
                path, position = self.drag_dest_info
                target = self.resolve_drop_target(path, position)
                if target:
                    success = self.handle_drop(payload, target)
        context.finish(success, False, time)
        self.drag_dest_info = None

    def handle_drop(self, payload: Dict[str, object], target: Dict[str, str]) -> bool:
        """Apply a drop of ``payload`` onto ``target``; returns whether something moved."""

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return False
        kind = payload.get("kind")
        if kind == RowKind.HOST.value:
            return self._drop_hosts(items, target)
        if kind == RowKind.CHILD.value:
            return self._drop_child_group(items, target)
        return False

    @requires_editable_inventory
    def _drop_hosts(self, items: list, target: Dict[str, str]) -> bool:
        win = self.win
        if target.get("type") == RowKind.CHILD.value:
            target_parent, target_child, create_child = target["parent"], target["child"], False
        elif target.get("type") == RowKind.PARENT.value:
            target_parent = target["parent"]
            choice = win.groups.prompt_child_destination(target_parent)
            if not choice:
                return False
            target_child, create_child = choice
        else:
            win._notify_info(_("Drop hosts onto a group row."))
            return False
        sources: List[Tuple[str, str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                return False
            parent, child, alias = item.get("parent"), item.get("child"), item.get("alias")
            if not all(isinstance(val, str) and val for val in (parent, child, alias)):
                return False
            if (parent, child) != (target_parent, target_child):
                sources.append((parent, child, alias))
        if not sources:
            win._notify_info(_("The hosts are already in that group."))
            return False
        success, msg = win.manager.move_hosts(sources, target_parent, target_child, create_child=create_child)
        win._log_event(msg, success)
        if success:
            for source_parent, source_child, alias in sources:
                win.creds.rebind_credential(
                    f"{source_parent}::{source_child}::{alias}",
                    f"{target_parent}::{target_child}::{alias}",
                )
            win.tree.rebuild(refresh=True)
            win.tree.select_child_group(target_parent, target_child)
        return success

    @requires_editable_inventory
    def _drop_child_group(self, items: list, target: Dict[str, str]) -> bool:
        win = self.win
        if len(items) != 1:
            win._notify_info(_("Drag one child group at a time."))
            return False
        source = items[0]
        if not isinstance(source, dict):
            return False
        source_parent, source_child = source.get("parent"), source.get("child")
        if not isinstance(source_parent, str) or not isinstance(source_child, str):
            return False
        if target.get("type") not in (RowKind.CHILD.value, RowKind.PARENT.value):
            return False
        target_parent = target["parent"]
        if source_parent == target_parent:
            win._notify_info(_("The child group is already in that parent group."))
            return False
        success, msg = win.manager.move_child_group(source_parent, source_child, target_parent)
        win._log_event(msg, success)
        if success:
            win.creds.remap_credential_prefix(f"{source_parent}::{source_child}::", f"{target_parent}::{source_child}::")
            win.tree.rebuild(refresh=True)
            win.tree.select_child_group(target_parent, source_child)
        return success
