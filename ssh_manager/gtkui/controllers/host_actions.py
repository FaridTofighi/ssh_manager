"""Context-menu descriptions for the session tree (GTK-free).

Menus are grouped into the same four sections everywhere — *Connect*,
*Edit*, *Organize* and *Destructive* — and built from :mod:`menu_spec`
objects. ``Reconnect`` only appears when a session for the host is already
open; hosts get their own *Change Color…* entry that recolors the host (not
its group).
"""

from __future__ import annotations

from typing import Dict, List, Protocol

from ..constants import Icons, RowKind
from ..i18n import _
from ..menu_spec import MenuItemSpec, MenuSection, MenuSpec


class TreeMenuCallbacks(Protocol):
    def _connect_selected(self) -> None: ...
    def _reconnect_selected(self) -> None: ...
    def _copy_ssh_command(self) -> None: ...
    def _copy_scp_command(self) -> None: ...
    def _edit_selected(self) -> None: ...
    def _duplicate_selected(self) -> None: ...
    def _move_selected(self) -> None: ...
    def _change_host_color(self) -> None: ...
    def _change_group_color(self) -> None: ...
    def _clear_group_color(self) -> None: ...
    def _forget_password_for_current_host(self) -> None: ...
    def _delete_selected(self) -> None: ...
    def _connect_child_group(self) -> None: ...
    def _connect_parent_group(self) -> None: ...
    def _create_child_group_for_current(self) -> None: ...
    def _create_host_in_current_group(self) -> None: ...
    def _rename_child_group(self) -> None: ...
    def _rename_parent_group(self) -> None: ...
    def _duplicate_child_group(self) -> None: ...
    def _duplicate_parent_group(self) -> None: ...
    def _delete_child_group(self) -> None: ...
    def _delete_parent_group(self) -> None: ...
    def prompt_new_host(self) -> None: ...
    def _create_parent_group(self) -> None: ...
    def _quick_connect(self) -> None: ...


def host_menu(
    cb: TreeMenuCallbacks,
    *,
    can_edit: bool = True,
    has_session: bool = False,
    accels: Dict[str, str] = None,  # type: ignore[assignment]
) -> MenuSpec:
    accels = accels or {}
    return [
        MenuSection("connect", [
            MenuItemSpec(_("_Connect"), cb._connect_selected, Icons.CONNECT, accels.get("host-connect", "")),
            MenuItemSpec(_("_Reconnect"), cb._reconnect_selected, Icons.RECONNECT, visible=has_session,
                         tooltip=_("Reconnect the open session for this host")),
            MenuItemSpec(_("Copy SSH Command"), cb._copy_ssh_command, Icons.TERMINAL),
            MenuItemSpec(_("Copy SCP Command"), cb._copy_scp_command, Icons.COPY),
        ]),
        MenuSection("edit", [
            MenuItemSpec(_("_Edit…"), cb._edit_selected, Icons.EDIT, accels.get("rename", ""), sensitive=can_edit),
            MenuItemSpec(_("D_uplicate…"), cb._duplicate_selected, Icons.DUPLICATE, sensitive=can_edit),
        ]),
        MenuSection("organize", [
            MenuItemSpec(_("_Move to Group…"), cb._move_selected, Icons.MOVE, sensitive=can_edit),
            MenuItemSpec(_("Change Host _Color…"), cb._change_host_color, Icons.COLOR),
            MenuItemSpec(_("Clear Host Color"), cb._clear_group_color, Icons.CLEAR),
            MenuItemSpec(_("_Forget Password"), cb._forget_password_for_current_host, Icons.FORGET),
        ]),
        MenuSection("destructive", [
            MenuItemSpec(_("_Delete Host…"), cb._delete_selected, Icons.DELETE, accels.get("host-delete", ""),
                         sensitive=can_edit, destructive=True),
        ]),
    ]


def group_menu(
    cb: TreeMenuCallbacks,
    kind: RowKind,
    *,
    read_only: bool = False,
    has_hosts: bool = True,
    accels: Dict[str, str] = None,  # type: ignore[assignment]
) -> MenuSpec:
    accels = accels or {}
    editable = not read_only
    connect = cb._connect_parent_group if kind is RowKind.PARENT else cb._connect_child_group
    sections: List[MenuSection] = [
        MenuSection("connect", [
            MenuItemSpec(_("Connect _All"), connect, Icons.CONNECT, sensitive=has_hosts),
        ]),
        MenuSection("edit", [
            MenuItemSpec(_("New _Host…"), cb._create_host_in_current_group, Icons.NEW, sensitive=editable),
            MenuItemSpec(_("New _Subgroup…"), cb._create_child_group_for_current, Icons.FOLDER, sensitive=editable),
        ]),
    ]
    if kind is RowKind.PARENT:
        sections[1].items += [
            MenuItemSpec(_("_Rename…"), cb._rename_parent_group, Icons.EDIT, accels.get("rename", ""), sensitive=editable),
            MenuItemSpec(_("D_uplicate…"), cb._duplicate_parent_group, Icons.DUPLICATE, sensitive=editable),
        ]
    elif kind is RowKind.CHILD:
        sections[1].items += [
            MenuItemSpec(_("_Rename…"), cb._rename_child_group, Icons.EDIT, accels.get("rename", ""), sensitive=editable),
            MenuItemSpec(_("D_uplicate…"), cb._duplicate_child_group, Icons.DUPLICATE, sensitive=editable),
        ]
    sections.append(MenuSection("organize", [
        MenuItemSpec(_("Change Group _Color…"), cb._change_group_color, Icons.COLOR),
        MenuItemSpec(_("Clear Group Color"), cb._clear_group_color, Icons.CLEAR),
    ]))
    if kind is RowKind.PARENT:
        sections.append(MenuSection("destructive", [
            MenuItemSpec(_("_Delete Group…"), cb._delete_parent_group, Icons.DELETE, accels.get("host-delete", ""),
                         sensitive=editable, destructive=True),
        ]))
    elif kind is RowKind.CHILD:
        sections.append(MenuSection("destructive", [
            MenuItemSpec(_("_Delete Child Group…"), cb._delete_child_group, Icons.DELETE, accels.get("host-delete", ""),
                         sensitive=editable, destructive=True),
        ]))
    return sections


def blank_menu(cb: TreeMenuCallbacks, *, accels: Dict[str, str] = None) -> MenuSpec:  # type: ignore[assignment]
    accels = accels or {}
    return [
        MenuSection("create", [
            MenuItemSpec(_("New _Host…"), cb.prompt_new_host, Icons.NEW, accels.get("new-host", "")),
            MenuItemSpec(_("New _Group…"), cb._create_parent_group, Icons.FOLDER),
        ]),
        MenuSection("connect", [
            MenuItemSpec(_("_Quick Connect…"), cb._quick_connect, Icons.CONNECT, accels.get("quick-connect", "")),
        ]),
    ]
