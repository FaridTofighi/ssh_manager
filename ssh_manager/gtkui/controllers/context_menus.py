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
    def connect_selected(self) -> None: ...
    def reconnect_selected(self) -> None: ...
    def copy_ssh_command(self) -> None: ...
    def copy_scp_command(self) -> None: ...
    def open_files_selected(self) -> None: ...
    def export_rdp_file(self) -> None: ...
    def export_group(self) -> None: ...
    def export_selected_hosts(self) -> None: ...
    def wake_and_connect(self) -> None: ...
    def edit_selected(self) -> None: ...
    def duplicate_selected(self) -> None: ...
    def move_selected(self) -> None: ...
    def change_host_color(self) -> None: ...
    def change_group_color(self) -> None: ...
    def clear_color(self) -> None: ...
    def forget_password(self) -> None: ...
    def delete_selected(self) -> None: ...
    def connect_child_group(self) -> None: ...
    def connect_parent_group(self) -> None: ...
    def create_child_group_for_current(self) -> None: ...
    def create_host_in_current_group(self) -> None: ...
    def rename_child_group(self) -> None: ...
    def rename_parent_group(self) -> None: ...
    def duplicate_child_group(self) -> None: ...
    def duplicate_parent_group(self) -> None: ...
    def delete_child_group(self) -> None: ...
    def delete_parent_group(self) -> None: ...
    def prompt_new_host(self) -> None: ...
    def create_parent_group(self) -> None: ...
    def quick_connect(self) -> None: ...
    def expand_all_groups(self) -> None: ...
    def collapse_all_groups(self) -> None: ...


def host_menu(
    cb: TreeMenuCallbacks,
    *,
    can_edit: bool = True,
    has_session: bool = False,
    is_ssh: bool = True,
    is_rdp: bool = False,
    can_wake: bool = False,
    accels: Dict[str, str] = None,  # type: ignore[assignment]
) -> MenuSpec:
    accels = accels or {}
    return [
        MenuSection("connect", [
            MenuItemSpec(_("_Connect"), cb.connect_selected, Icons.CONNECT, accels.get("host-connect", "")),
            MenuItemSpec(_("_Reconnect"), cb.reconnect_selected, Icons.RECONNECT, visible=has_session,
                         tooltip=_("Reconnect the open session for this host")),
            MenuItemSpec(_("Browse _Files (SFTP)"), cb.open_files_selected, Icons.TRANSFER, visible=is_ssh,
                         tooltip=_("Open a file browser over SFTP with this host's address and login")),
            MenuItemSpec(_("_Wake and Connect"), cb.wake_and_connect, Icons.CONNECT, visible=is_rdp and can_wake,
                         tooltip=_("Send a Wake-on-LAN packet, then connect")),
            MenuItemSpec(_("Export as ._rdp File…"), cb.export_rdp_file, Icons.SAVE, visible=is_rdp,
                         tooltip=_("For other Remote Desktop clients; no password is stored")),
            MenuItemSpec(_("Copy SSH Command"), cb.copy_ssh_command, Icons.TERMINAL),
            MenuItemSpec(_("Copy SCP Command"), cb.copy_scp_command, Icons.COPY),
        ]),
        MenuSection("edit", [
            MenuItemSpec(_("_Edit…"), cb.edit_selected, Icons.EDIT, accels.get("rename", ""), sensitive=can_edit),
            MenuItemSpec(_("D_uplicate…"), cb.duplicate_selected, Icons.DUPLICATE, sensitive=can_edit),
        ]),
        MenuSection("organize", [
            MenuItemSpec(_("_Move to Group…"), cb.move_selected, Icons.MOVE, sensitive=can_edit),
            MenuItemSpec(_("Change Host _Color…"), cb.change_host_color, Icons.COLOR),
            MenuItemSpec(_("Clear Host Color"), cb.clear_color, Icons.CLEAR),
            MenuItemSpec(_("_Forget Password"), cb.forget_password, Icons.FORGET),
            MenuItemSpec(_("E_xport Selected Hosts…"), cb.export_selected_hosts, Icons.SAVE,
                         tooltip=_("As an SSH Manager file (.sshm); no passwords")),
        ]),
        MenuSection("destructive", [
            MenuItemSpec(_("_Delete Host…"), cb.delete_selected, Icons.DELETE, accels.get("host-delete", ""),
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
    connect = cb.connect_parent_group if kind is RowKind.PARENT else cb.connect_child_group
    sections: List[MenuSection] = [
        MenuSection("connect", [
            MenuItemSpec(_("Connect _All"), connect, Icons.CONNECT, sensitive=has_hosts),
        ]),
        MenuSection("edit", [
            MenuItemSpec(_("New _Host…"), cb.create_host_in_current_group, Icons.NEW, sensitive=editable),
            MenuItemSpec(_("New _Subgroup…"), cb.create_child_group_for_current, Icons.FOLDER, sensitive=editable),
        ]),
    ]
    if kind is RowKind.PARENT:
        sections[1].items += [
            MenuItemSpec(_("_Rename…"), cb.rename_parent_group, Icons.EDIT, accels.get("rename", ""), sensitive=editable),
            MenuItemSpec(_("D_uplicate…"), cb.duplicate_parent_group, Icons.DUPLICATE, sensitive=editable),
        ]
    elif kind is RowKind.CHILD:
        sections[1].items += [
            MenuItemSpec(_("_Rename…"), cb.rename_child_group, Icons.EDIT, accels.get("rename", ""), sensitive=editable),
            MenuItemSpec(_("D_uplicate…"), cb.duplicate_child_group, Icons.DUPLICATE, sensitive=editable),
        ]
    sections.append(MenuSection("organize", [
        MenuItemSpec(_("Change Group _Color…"), cb.change_group_color, Icons.COLOR),
        MenuItemSpec(_("Clear Group Color"), cb.clear_color, Icons.CLEAR),
        MenuItemSpec(_("E_xport Group…"), cb.export_group, Icons.SAVE,
                     tooltip=_("This group and its subgroups as an SSH Manager file (.sshm); no passwords")),
    ]))
    if kind is RowKind.PARENT:
        sections.append(MenuSection("destructive", [
            MenuItemSpec(_("_Delete Group…"), cb.delete_parent_group, Icons.DELETE, accels.get("host-delete", ""),
                         sensitive=editable, destructive=True),
        ]))
    elif kind is RowKind.CHILD:
        sections.append(MenuSection("destructive", [
            MenuItemSpec(_("_Delete Child Group…"), cb.delete_child_group, Icons.DELETE, accels.get("host-delete", ""),
                         sensitive=editable, destructive=True),
        ]))
    return sections


def blank_menu(cb: TreeMenuCallbacks, *, accels: Dict[str, str] = None) -> MenuSpec:  # type: ignore[assignment]
    accels = accels or {}
    return [
        MenuSection("create", [
            MenuItemSpec(_("New _Host…"), cb.prompt_new_host, Icons.NEW, accels.get("new-host", "")),
            MenuItemSpec(_("New _Group…"), cb.create_parent_group, Icons.FOLDER),
        ]),
        MenuSection("connect", [
            MenuItemSpec(_("_Quick Connect…"), cb.quick_connect, Icons.CONNECT, accels.get("quick-connect", "")),
        ]),
        MenuSection("view", [
            MenuItemSpec(_("_Expand All Groups"), cb.expand_all_groups, Icons.CHEVRON_DOWN,
                         accels.get("expand-all-groups", "")),
            MenuItemSpec(_("C_ollapse All Groups"), cb.collapse_all_groups, Icons.CHEVRON_RIGHT,
                         accels.get("collapse-all-groups", "")),
        ]),
    ]
