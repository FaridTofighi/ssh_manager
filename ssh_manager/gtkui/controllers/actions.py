"""``WindowActions``: the user-facing commands that views and menus can trigger.

Context menus and the details presenter only know these method names; the
facade routes each one to the controller that implements it, so views never
reach into controller internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


class WindowActions:
    """Named commands of the main window."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win

    # sessions
    def connect_selected(self) -> None:
        self.win.sessions.connect_selected()

    def reconnect_selected(self) -> None:
        self.win.sessions.reconnect_selected()

    def connect_child_group(self) -> None:
        self.win.sessions.connect_child_group()

    def connect_parent_group(self) -> None:
        self.win.sessions.connect_parent_group()

    def bulk_connect_selected(self) -> None:
        self.win.sessions.bulk_connect_selected()

    def copy_ssh_command(self) -> None:
        self.win.sessions.copy_ssh_command()

    def copy_scp_command(self) -> None:
        self.win.sessions.copy_scp_command()

    def open_files_selected(self) -> None:
        self.win.sessions.open_files_selected()

    def export_rdp_file(self) -> None:
        self.win.rdp_files.export_selected()

    def export_group(self) -> None:
        self.win.native.export("groups")

    def export_selected_hosts(self) -> None:
        self.win.native.export("hosts")

    def wake_and_connect(self) -> None:
        self.win.rdp_files.wake_and_connect()

    def quick_connect(self) -> None:
        self.win.sessions.quick_connect()

    # hosts
    def prompt_new_host(self) -> None:
        self.win.hosts.prompt_new_host()

    def edit_selected(self) -> None:
        self.win.hosts.edit_selected()

    def duplicate_selected(self) -> None:
        self.win.hosts.duplicate_selected()

    def move_selected(self) -> None:
        self.win.hosts.move_selected()

    def delete_selected(self) -> None:
        self.win.hosts.delete_selected()

    def bulk_move_selected(self) -> None:
        self.win.hosts.bulk_move_selected()

    def bulk_tag_selected(self) -> None:
        self.win.hosts.bulk_tag_selected()

    def create_host_in_current_group(self) -> None:
        self.win.hosts.create_host_in_current_group()

    def edit_current_item(self) -> None:
        self.win.hosts.edit_current_item()

    def delete_current_item(self) -> None:
        self.win.hosts.delete_current_item()

    def forget_password(self) -> None:
        self.win.creds.forget_password()

    # groups
    def expand_all_groups(self) -> None:
        self.win.tree.expand_all_groups()

    def collapse_all_groups(self) -> None:
        self.win.tree.collapse_all_groups()

    def create_parent_group(self) -> None:
        self.win.groups.create_parent_group()

    def create_child_group_for_current(self) -> None:
        self.win.groups.create_child_group_for_current()

    def rename_parent_group(self) -> None:
        self.win.groups.rename_parent_group()

    def rename_child_group(self) -> None:
        self.win.groups.rename_child_group()

    def duplicate_parent_group(self) -> None:
        self.win.groups.duplicate_parent_group()

    def duplicate_child_group(self) -> None:
        self.win.groups.duplicate_child_group()

    def delete_parent_group(self) -> None:
        self.win.groups.delete_parent_group()

    def delete_child_group(self) -> None:
        self.win.groups.delete_child_group()

    # colors
    def change_host_color(self) -> None:
        self.win.colors.change_host_color()

    def change_group_color(self) -> None:
        self.win.colors.change_item_color()

    def clear_color(self) -> None:
        self.win.colors.clear_item_color()
