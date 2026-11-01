"""Textual UI for the SSH manager."""

from __future__ import annotations

import inspect
import asyncio
import re
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
import os
import uuid

from rich.panel import Panel
from rich.table import Table
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Log,
    Static,
    Tree,
    TabPane,
    TabbedContent,
)
from textual.widgets.tree import TreeNode

from .config import ConfigManager, GroupConfig, HostEntry
from .launcher import ConnectionLauncher
from .terminal import TerminalSession, TerminalWidget
from .ssh_command import build_ssh_command
from .host_utils import host_option


HOST_FORM_BASE_OPTIONS = {"hostname", "user", "port", "identityfile"}
_EXTRA_OPTION_SEPARATOR = re.compile(
    r",\s*(?=[A-Za-z][A-Za-z0-9_-]*\s*=)|,\s+"
)


def format_extra_options(options: Mapping[str, Any]) -> str:
    return ", ".join(
        f"{key}={value}"
        for key, value in options.items()
        if str(key).lower() not in HOST_FORM_BASE_OPTIONS
    )


def parse_extra_options(raw: str) -> Tuple[Dict[str, str], Optional[str]]:
    parsed: Dict[str, str] = {}
    if not raw.strip():
        return parsed, None
    for chunk in _EXTRA_OPTION_SEPARATOR.split(raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            return {}, f"Invalid extra option '{chunk}'. Use key=value."
        key, value = chunk.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not key:
            return {}, "Extra option key cannot be empty."
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in key):
            return {}, f"Extra option key '{key}' must not contain whitespace or control characters."
        if "\n" in value or "\r" in value:
            return {}, f"Extra option '{key}' must not contain line breaks."
        parsed[key] = value
    return parsed, None


class ConfirmModal(ModalScreen[bool]):
    """Modal dialog with confirm/cancel buttons."""

    def __init__(
        self,
        title: str,
        message: str,
        confirm_label: str = "Delete",
        cancel_label: str = "Cancel",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        self.context_data = context or {}

    def compose(self) -> ComposeResult:
        with Container(id="modal"):
            yield Static(f"[b]{self._title}[/b]", id="modal-title")
            yield Static(self._message, id="modal-prompt")
            with Horizontal(id="modal-buttons"):
                yield Button(self._cancel_label, id="cancel")
                yield Button(self._confirm_label, id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss(False)


class TextInputModal(ModalScreen[Optional[str]]):
    """Modal dialog that captures a single line of text input."""

    def __init__(
        self,
        title: str,
        prompt: str,
        initial: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._initial = initial
        self.context_data = context or {}

    def compose(self) -> ComposeResult:
        with Container(id="modal"):
            yield Static(f"[b]{self._title}[/b]", id="modal-title")
            yield Static(self._prompt, id="modal-prompt")
            yield Input(value=self._initial, id="modal-input")
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        value = self.query_one(Input).value.strip()
        self.dismiss(value or None)


class HostForm(ModalScreen[Optional[Dict[str, Any]]]):
    """Modal dialog for adding or updating host entries."""

    def __init__(
        self,
        title: str,
        parent: str,
        child: str,
        initial: Optional[HostEntry] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._parent = parent
        self._child = child
        self._initial = initial
        self.context_data = context or {}
        self._error: Optional[Static] = None

    def compose(self) -> ComposeResult:
        initial = self._initial or HostEntry(
            alias="",
            options={},
            parent_group=self._parent,
            child_group=self._child,
        )
        with Container(id="modal"):
            yield Static(f"[b]{self._title}[/b]", id="modal-title")
            yield Static(f"{self._parent}/{self._child}", id="modal-subtitle")
            yield Input(initial.alias, placeholder="Alias (required)", id="alias")
            yield Input(
                host_option(initial, "hostname") or "",
                placeholder="Hostname",
                id="hostname",
            )
            yield Input(
                host_option(initial, "user") or "",
                placeholder="User",
                id="user",
            )
            yield Input(
                host_option(initial, "port") or "",
                placeholder="Port",
                id="port",
            )
            yield Input(
                host_option(initial, "identityfile") or "",
                placeholder="IdentityFile",
                id="identityfile",
            )
            extras = format_extra_options(initial.options)
            yield Input(
                extras,
                placeholder="Extra options (key=value, comma separated)",
                id="extras",
            )
            self._error = Static("", id="modal-error")
            yield self._error
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#alias", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        alias = self.query_one("#alias", Input).value.strip()
        if not alias:
            self._set_error("Alias is required.")
            return
        hostname = self.query_one("#hostname", Input).value.strip()
        user = self.query_one("#user", Input).value.strip()
        port = self.query_one("#port", Input).value.strip()
        identity = self.query_one("#identityfile", Input).value.strip()
        extras_raw = self.query_one("#extras", Input).value

        options: Dict[str, str] = {}
        if hostname:
            options["hostname"] = hostname
        if user:
            options["user"] = user
        if port:
            options["port"] = port
        if identity:
            options["identityfile"] = identity
        extra_options, error = parse_extra_options(extras_raw)
        if error:
            self._set_error(error)
            return
        options.update(extra_options)

        payload = {"alias": alias, "options": options}
        self.dismiss(payload)

    def _set_error(self, message: str) -> None:
        if self._error:
            self._error.update(f"[red]{message}")


class MoveTargetModal(ModalScreen[Optional[Dict[str, Any]]]):
    """Modal dialog for picking a destination from existing groups."""

    def __init__(
        self,
        title: str,
        message: str,
        tree_data: Dict[str, List[str]],
        require_child: bool,
        current_parent: str,
        current_child: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self.tree_data = tree_data
        self.require_child = require_child
        self.current_parent = current_parent
        self.current_child = current_child
        self._status: Optional[Static] = None
        self._confirm: Optional[Button] = None
        self._selection: Optional[Dict[str, str]] = None
        self._tree: Optional[Tree] = None

    def compose(self) -> ComposeResult:
        with Container(id="modal"):
            yield Static(f"[b]{self._title}[/b]", id="modal-title")
            yield Static(self._message, id="modal-info")
            tree = Tree(":globe_with_meridians: Destinations", id="move-tree")
            root = tree.root
            if root is None:
                root = tree.root = tree.add(":globe_with_meridians: Destinations")
            else:
                root.set_label(":globe_with_meridians: Destinations")
                for child in list(root.children):
                    child.remove()

            for parent in sorted(self.tree_data):
                parent_node = root.add(
                    f":open_file_folder: [b]{parent}[/b]",
                    data={"kind": "parent", "parent": parent},
                )
                for child in self.tree_data[parent]:
                    parent_node.add(
                        f":page_facing_up: {child}",
                        data={
                            "kind": "child",
                            "parent": parent,
                            "child": child,
                        },
                    )
                parent_node.expand()
            tree.show_root = False
            self._tree = tree
            yield tree
            hint_text = (
                "[dim]Use ↑↓ to pick a subgroup, Enter to confirm.[/dim]"
                if self.require_child
                else "[dim]Use ↑↓ to pick a parent directory, Enter to confirm.[/dim]"
            )
            self._status = Static(hint_text, id="modal-status")
            yield self._status
            with Horizontal(id="modal-buttons"):
                yield Button("Cancel", id="cancel")
                self._confirm = Button(
                    "Move Here",
                    id="confirm",
                    variant="primary",
                    disabled=True,
                )
                yield self._confirm

    def on_mount(self) -> None:
        if self._tree:
            self._tree.focus()
            self._highlight_default()
        if not self.tree_data:
            self._set_status("No destinations available.", error=True)
            self._set_confirm_enabled(False)
        elif self.require_child and not any(self.tree_data.values()):
            self._set_status("No subgroups available at the moment.", error=True)
            self._set_confirm_enabled(False)

    def _highlight_default(self) -> None:
        if not self._tree:
            return
        for parent_node in self._tree.root.children if self._tree.root else []:
            parent = parent_node.data.get("parent") if parent_node.data else None
            if parent == self.current_parent:
                self._tree.select_node(parent_node)
                if self.require_child and self.current_child:
                    for child_node in parent_node.children:
                        child = child_node.data.get("child") if child_node.data else None
                        if child == self.current_child:
                            self._tree.select_node(child_node)
                            self._apply_selection(child_node)
                            break
                else:
                    if not self.require_child:
                        self._apply_selection(parent_node)
                break

    def _apply_selection(self, node: TreeNode, *, announce: bool = True) -> None:
        data = node.data or {}
        kind = data.get("kind")
        if self.require_child:
            if kind != "child":
                self._selection = None
                self._set_status("Select a subgroup (child config) to continue.", error=True)
                self._set_confirm_enabled(False)
                return
            self._selection = {"parent": data["parent"], "child": data["child"]}
            if announce:
                self._set_status(
                    f"Move to [b]{data['parent']}/{data['child']}[/b].",
                    error=False,
                )
        else:
            if kind != "parent":
                self._selection = None
                if announce:
                    self._set_status("Select a parent directory.", error=True)
                self._set_confirm_enabled(False)
                return
            self._selection = {"parent": data["parent"]}
            if announce:
                self._set_status(
                    f"Move under parent [b]{data['parent']}[/b].",
                    error=False,
                )
        self._set_confirm_enabled(True)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        self._apply_selection(event.node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._apply_selection(event.node, announce=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "confirm":
            self._submit()

    def on_key(self, event: Key) -> None:
        if event.key == "enter" and self._confirm and not self._confirm.disabled:
            event.stop()
            self._submit()
        elif event.key == "escape":
            event.stop()
            self.dismiss(None)

    def _set_status(self, message: str, *, error: bool) -> None:
        if not self._status:
            return
        prefix = "[red]" if error else "[cyan]"
        self._status.update(f"{prefix}{message}")

    def _set_confirm_enabled(self, enabled: bool) -> None:
        if self._confirm:
            self._confirm.disabled = not enabled

    def _submit(self) -> None:
        if not self._selection:
            self._set_status("Select a destination before moving.", error=True)
            self._set_confirm_enabled(False)
            return
        self.dismiss(dict(self._selection))


class SSHManagerApp(App):
    """Main Textual application."""

    CSS = """
    Screen {
        layout: vertical;
        background: #06080f;
        color: #e2e8f0;
    }

    #content {
        layout: horizontal;
        height: 1fr;
        padding: 1;
    }

    #left-pane {
        layout: vertical;
        width: 38%;
        border: round #1f2a40;
        background: #0f172a;
        padding: 1;
    }

    #search-box {
        margin-bottom: 1;
        height: 3;
        border: round #2563eb;
        background: #0b1120;
        color: #e2e8f0;
        padding-left: 1;
        padding-right: 1;
    }

    #tree {
        width: 100%;
        border: none;
        background: transparent;
        color: #cbd5f5;
    }

    Tree > .label {
        color: #94a3b8;
    }

    Tree > .label.--cursor {
        background: #1d4ed8 20%;
        color: #e2e8f0;
    }

    #right-pane {
        layout: vertical;
        border: round #1f2a40;
        background: #0b1120;
        padding: 1;
        margin-left: 1;
    }

    #actions {
        height: auto;
        padding: 0 1 1 1;
        border-bottom: hkey #192338;
        align-horizontal: left;
        dock: top;
        margin-bottom: 1;
    }

    #actions Button {
        margin-right: 1;
    }

    Button {
        border: round #1f2937;
        background: #1e293b;
        color: #e2e8f0;
        padding: 0 1;
        min-width: 10;
        height: auto;
        text-style: bold;
    }

    Button:hover {
        background: #27324a;
        border: round #2563eb;
    }

    Button.-primary {
        border: round #1f2937;
        background: #1f2a44;
        color: #f8fafc;
    }

    Button.-primary:hover {
        background: #243356;
        border: round #38bdf8;
    }

    Button:focus {
        border: round #38bdf8;
        background: #1e40af;
        color: #f8fafc;
    }

    Button.-primary:focus {
        border: round #38bdf8;
        background: #1e40af;
        color: #f8fafc;
    }

    #details {
        height: 2fr;
        padding: 1;
        border: round #1f2a40;
        background: #0f172a;
        margin-bottom: 1;
    }

    #event-log {
        height: 1fr;
        border: round #1f2a40;
        background: #060b16;
    }

    #main-tabs {
        height: 1fr;
        border: round #1f2a40;
        background: #060b16;
        margin-top: 1;
    }

    #overview-pane {
        layout: vertical;
        height: 1fr;
        padding: 1;
        background: transparent;
    }

    #terminal-tabs {
        height: 1fr;
        border: none;
        background: #050b1a;
    }

    #terminal-tabs TabPane {
        height: 1fr;
    }

    #modal {
        width: 60%;
        max-width: 68;
        padding: 1;
        border: round #2563eb;
        background: #0f172a;
        color: #e2e8f0;
    }

    #modal-title {
        margin-bottom: 1;
        color: #60a5fa;
    }

    #modal-subtitle, #modal-prompt {
        margin-bottom: 1;
        color: #94a3b8;
    }

    #modal-info {
        margin-bottom: 1;
        color: #93c5fd;
    }

    #modal-error {
        height: auto;
        color: #f87171;
    }

    #modal-buttons {
        align-horizontal: right;
        padding-top: 1;
    }

    #modal-buttons Button {
        margin-left: 1;
    }

    #move-tree {
        height: 14;
        border: round #1f2a40;
        background: #111d35;
        padding: 1;
        margin-bottom: 1;
    }

    #modal-status {
        min-height: 1;
        margin-bottom: 1;
        color: #8ab4f8;
    }
    """

    def __init__(
        self,
        manager: ConfigManager,
        launcher: ConnectionLauncher,
        base_dir: str,
    ) -> None:
        super().__init__()
        self.manager = manager
        self.launcher = launcher
        self.base_dir = base_dir
        self.data: Dict[str, Dict[str, GroupConfig]] = manager.tree
        self.selected_host: Optional[HostEntry] = None
        self.selected_hosts: Set[Tuple[str, str, str]] = set()
        self.search_query: Optional[str] = None
        self._matched_parents: Set[str] = set()
        self._matched_children: Set[Tuple[str, str]] = set()
        self._matched_hosts: Set[Tuple[str, str, str]] = set()
        self._search_result_count: int = 0
        self.left_width: float = 0.38
        self.left_collapsed: bool = False
        self._stored_left_width: float = self.left_width
        self.terminal_tabs_map: dict[str, TerminalWidget] = {}
        self._modal_active = False

    # ----------------------------------------------------------------- lifecycle
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="content"):
            with Vertical(id="left-pane"):
                yield Input(
                    "",
                    placeholder="Search hosts, subgroups, or parents...",
                    id="search-box",
                    select_on_focus=False,
                )
                yield Tree(":globe_with_meridians: SSH Hosts", id="tree")
            with Vertical(id="right-pane"):
                with Horizontal(id="actions"):
                    yield Button("Add Parent", id="btn-add-parent", variant="primary")
                    yield Button("Add Child", id="btn-add-child")
                    yield Button("Add Host", id="btn-add-host")
                    yield Button("Edit", id="btn-edit")
                with TabbedContent(id="main-tabs", initial="overview"):
                    with TabPane("Overview", id="overview"):
                        with Vertical(id="overview-pane"):
                            yield Static("Select a host to see details.", id="details")
                            yield Log(id="event-log")
                    with TabPane("Terminals", id="terminals"):
                        with TabbedContent(id="terminal-tabs"):
                            yield TabPane(
                                "Sessions",
                                Static(
                                    "No sessions yet. Launch an SSH connection to open a terminal tab.",
                                    id="terminal-placeholder-body",
                                ),
                                id="terminal-placeholder",
                            )
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#tree", Tree).focus()
        await self.action_reload()

    # ------------------------------------------------------------------- log util
    def _log_event(self, success: bool, message: str) -> None:
        log = self.query_one("#event-log", Log)
        prefix = "[green]" if success else "[red]"
        log.write(f"{prefix}{message}")

    def _ensure_tree_focus(self) -> None:
        tree = self.query_one("#tree", Tree)
        if not tree.has_focus:
            tree.focus()

    def _modal_open(self) -> bool:
        return self._modal_active

    @staticmethod
    def _host_key(host: HostEntry) -> Tuple[str, str, str]:
        return (host.parent_group, host.child_group, host.alias)

    def _format_host_label(self, host: HostEntry) -> str:
        key = self._host_key(host)
        selected = key in self.selected_hosts
        matched = key in self._matched_hosts
        if selected:
            icon = ":check_mark_button:"
        elif matched:
            icon = ":mag_right:"
        else:
            icon = ":electric_plug:"
        color = "yellow" if matched else "green"
        weight = "bold " if matched else ""
        return f"{icon} [{color}]{host.alias}[/{color}]"

    def _toggle_host_selection(self, host: HostEntry) -> bool:
        key = self._host_key(host)
        if key in self.selected_hosts:
            self.selected_hosts.remove(key)
            return False
        self.selected_hosts.add(key)
        return True

    def _resolve_selected_hosts(self) -> List[HostEntry]:
        hosts: List[HostEntry] = []
        for parent, child, alias in self.selected_hosts:
            group = self.data.get(parent, {}).get(child)
            if not group:
                continue
            for host in group.hosts:
                if host.alias == alias:
                    hosts.append(host)
                    break
        return hosts

    def _terminal_tabs(self) -> TabbedContent:
        return self.query_one("#terminal-tabs", TabbedContent)

    def _main_tabs(self) -> TabbedContent:
        return self.query_one("#main-tabs", TabbedContent)

    def _ensure_terminal_placeholder(self) -> None:
        tabs = self._terminal_tabs()
        panes = [pane for pane in tabs.query(TabPane) if pane.id != "terminal-placeholder"]
        if not panes:
            tabs.add_pane(
                TabPane(
                    "Terminals",
                    Static(
                        "No sessions yet. Launch an SSH connection to open a terminal tab.",
                        id="terminal-placeholder-body",
                    ),
                    id="terminal-placeholder",
                )
            )
        if not panes:
            tabs.active = "terminal-placeholder"

    def _remove_terminal_placeholder(self) -> None:
        tabs = self._terminal_tabs()
        try:
            tabs.remove_pane("terminal-placeholder")
        except Exception:
            pass

    def _open_terminal_tab(self, host: HostEntry) -> bool:
        argv = build_ssh_command(host)
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        try:
            session = TerminalSession(argv, env=env)
        except Exception as exc:
            self._log_event(False, f"Failed to start embedded terminal: {exc}")
            return False

        pane_id = f"terminal-{uuid.uuid4().hex}"
        widget = TerminalWidget(session=session, pane_id=pane_id, title=host.display_title())
        tabs = self._terminal_tabs()
        self._remove_terminal_placeholder()
        pane_label = f":computer: {host.alias}"
        pane = TabPane(pane_label, widget, id=pane_id)
        tabs.add_pane(pane)
        tabs.active = pane_id
        self.terminal_tabs_map[pane_id] = widget
        self._main_tabs().active = "terminals"
        self._log_event(True, f"Opened {host.alias} in embedded terminal.")
        return True

    def _invoke_action(self, handler) -> None:
        if self._modal_open():
            return
        self._ensure_tree_focus()
        result = handler()
        if inspect.isawaitable(result):
            asyncio.create_task(result)

    def _open_text_modal(
        self,
        *,
        action: str,
        title: str,
        prompt: str,
        initial: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        context = {"action": action}
        if extra:
            context.update(extra)
        screen = TextInputModal(title, prompt, initial, context)
        self._ensure_tree_focus()
        self._modal_active = True

        def callback(result: Optional[str]) -> None:
            self._modal_active = False
            self._handle_text_result(context, result)
            self._ensure_tree_focus()

        self.push_screen(screen, callback)

    def _open_host_modal(
        self,
        *,
        action: str,
        title: str,
        parent: str,
        child: str,
        initial: Optional[HostEntry] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        context = {"action": action}
        if extra:
            context.update(extra)
        screen = HostForm(title, parent, child, initial=initial, context=context)
        self._ensure_tree_focus()
        self._modal_active = True

        def callback(result: Optional[Dict[str, Any]]) -> None:
            self._modal_active = False
            self._handle_host_result(context, result)
            self._ensure_tree_focus()

        self.push_screen(screen, callback)

    def _open_confirm_modal(
        self,
        *,
        action: str,
        title: str,
        message: str,
        confirm_label: str = "Delete",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        context = {"action": action}
        if extra:
            context.update(extra)
        screen = ConfirmModal(
            title,
            message,
            confirm_label=confirm_label,
            context=context,
        )
        self._ensure_tree_focus()
        self._modal_active = True

        def callback(result: bool) -> None:
            self._modal_active = False
            self._handle_confirm_result(context, result)
            self._ensure_tree_focus()

        self.push_screen(screen, callback)

    def _open_move_hosts_modal(
        self,
        host_keys: Set[Tuple[str, str, str]],
    ) -> None:
        if not host_keys:
            return
        child_map = {
            parent: sorted(groups.keys())
            for parent, groups in self.data.items()
            if groups
        }
        if not child_map:
            self._log_event(False, "No destination subgroups available for move.")
            return
        sample_parent, sample_child, _ = next(iter(host_keys))
        screen = MoveTargetModal(
            title="Move Hosts",
            message=(
                f"Move [bold]{len(host_keys)}[/bold] host(s). "
                "Pick the destination subgroup."
            ),
            tree_data=child_map,
            require_child=True,
            current_parent=sample_parent,
            current_child=sample_child,
        )
        self._ensure_tree_focus()
        self._modal_active = True

        def callback(result: Optional[Dict[str, Any]]) -> None:
            self._modal_active = False
            self._handle_move_hosts(host_keys, result)
            self._ensure_tree_focus()

        self.push_screen(screen, callback)

    def _open_move_child_modal(
        self,
        parent: str,
        child: str,
    ) -> None:
        tree_data = {
            p: sorted(groups.keys())
            for p, groups in self.data.items()
        }
        if not tree_data:
            self._log_event(False, "No parent groups available for move.")
            return
        screen = MoveTargetModal(
            title="Move Subgroup",
            message=(
                f"Move subgroup [bold]{parent}/{child}[/bold]. "
                "Select the destination parent directory."
            ),
            tree_data=tree_data,
            require_child=False,
            current_parent=parent,
        )
        self._ensure_tree_focus()
        self._modal_active = True

        group = self.data.get(parent, {}).get(child)
        host_aliases = [host.alias for host in group.hosts] if group else []

        def callback(result: Optional[Dict[str, Any]]) -> None:
            self._modal_active = False
            self._handle_move_child(parent, child, host_aliases, result)
            self._ensure_tree_focus()

        self.push_screen(screen, callback)

    # ---------------------------------------------------------------- tree update
    async def action_reload(self) -> None:
        self._rebuild_tree(announce=True)

    def _rebuild_tree(self, announce: bool = False) -> None:
        self.manager.refresh()
        self.data = self.manager.tree
        available_keys = {
            self._host_key(host)
            for groups in self.data.values()
            for group in groups.values()
            for host in group.hosts
        }
        self.selected_hosts &= available_keys
        tree = self.query_one("#tree", Tree)
        root = tree.root
        if root is None:
            root = tree.root = tree.add(":globe_with_meridians: SSH Hosts")
        else:
            root.set_label(":globe_with_meridians: SSH Hosts")
            for child in list(root.children):
                child.remove()

        filter_text = (self.search_query or "").strip().lower()
        is_filtered = bool(filter_text)
        matched_parents: Set[str] = set()
        matched_children: Set[Tuple[str, str]] = set()
        matched_hosts: Set[Tuple[str, str, str]] = set()
        search_count = 0

        def host_matches(host: HostEntry) -> bool:
            if not is_filtered:
                return False
            if filter_text in host.alias.lower():
                return True
            for value in host.options.values():
                if filter_text in str(value).lower():
                    return True
            return False

        for parent_name, children in self.data.items():
            parent_match = is_filtered and filter_text in parent_name.lower()
            child_nodes: List[Tuple[str, bool, List[Tuple[HostEntry, bool]]]] = []

            for child_name, group in children.items():
                child_match = is_filtered and filter_text in child_name.lower()
                hosts_for_child: List[Tuple[HostEntry, bool]] = []
                for host in group.hosts:
                    host_match = host_matches(host)
                    if not is_filtered:
                        hosts_for_child.append((host, False))
                    elif child_match:
                        hosts_for_child.append((host, host_match))
                        if host_match:
                            matched_hosts.add(self._host_key(host))
                            search_count += 1
                    elif host_match:
                        hosts_for_child.append((host, True))
                        matched_hosts.add(self._host_key(host))
                        search_count += 1

                include_child = (
                    not is_filtered
                    or child_match
                    or bool(hosts_for_child)
                )
                if include_child:
                    matched_in_child = child_match or any(match for _, match in hosts_for_child)
                    if is_filtered and matched_in_child:
                        matched_children.add((parent_name, child_name))
                    if is_filtered and child_match:
                        search_count += 1
                    child_nodes.append((child_name, matched_in_child, hosts_for_child))

            highlight_parent = (
                parent_match
                or any(
                    child_match or any(match for _, match in hosts_data)
                    for _, child_match, hosts_data in child_nodes
                )
            )
            include_parent = (
                not is_filtered
                or parent_match
                or bool(child_nodes)
            )
            if not include_parent:
                continue
            if is_filtered and highlight_parent:
                matched_parents.add(parent_name)
            if is_filtered and parent_match:
                search_count += 1

            parent_label = (
                f"[bold yellow]{parent_name}[/bold yellow]"
                if parent_name in matched_parents
                else f"[bold]{parent_name}[/bold]"
            )
            parent_node = root.add(
                f":open_file_folder: {parent_label}",
                expand=True,
                data={"kind": "parent", "name": parent_name},
            )
            for child_name, child_match, hosts_data in child_nodes:
                child_color = "yellow" if child_match else "cyan"
                child_node = parent_node.add(
                    f":page_facing_up: [{child_color}]{child_name}[/{child_color}]",
                    expand=is_filtered,
                    data={
                        "kind": "child",
                        "name": child_name,
                        "parent": parent_name,
                    },
                )
                for host, _ in hosts_data if hosts_data else []:
                    child_node.add(
                        self._format_host_label(host),
                        data={"kind": "host", "host": host},
                    )
        tree.refresh(layout=True)
        if is_filtered and search_count > 0:
            self._focus_first_match(matched_parents, matched_children, matched_hosts)

        self._matched_parents = matched_parents
        self._matched_children = matched_children
        self._matched_hosts = matched_hosts if is_filtered else set()
        self._search_result_count = search_count
        self._show_default_details()
        if announce:
            self._log_event(True, f"Loaded configuration from {self.base_dir}")
        self.query_one("#tree", Tree).focus()
        self._apply_pane_width()

    def _focus_first_match(
        self,
        matched_parents: Set[str],
        matched_children: Set[Tuple[str, str]],
        matched_hosts: Set[Tuple[str, str, str]],
    ) -> None:
        tree = self.query_one("#tree", Tree)
        root = tree.root
        if root is None:
            return

        def select(node: TreeNode) -> None:
            tree.select_node(node)
            tree.focus()
            try:
                tree.scroll_to_node(node)
            except AttributeError:
                pass

        for parent_node in root.children:
            parent_name = parent_node.data.get("name") if parent_node.data else ""
            for child_node in parent_node.children:
                child_name = child_node.data.get("name") if child_node.data else ""
                for host_node in child_node.children:
                    host = host_node.data.get("host") if host_node.data else None
                    if host and self._host_key(host) in matched_hosts:
                        select(host_node)
                        return
            for child_node in parent_node.children:
                child_name = child_node.data.get("name") if child_node.data else ""
                if (parent_name, child_name) in matched_children:
                    select(child_node)
                    return
            if parent_name in matched_parents:
                select(parent_node)
                return

    # --------------------------------------------------------------- tree events
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node = event.node
        kind = node.data.get("kind") if node.data else None
        if kind == "host":
            host = node.data["host"]
            self.selected_host = host
            self._show_host_details(host)
        elif kind == "child":
            self.selected_host = None
            self._show_group_details(node)
        else:
            self.selected_host = None
            self._show_default_details()

    def _current_node(self) -> Optional[TreeNode]:
        tree = self.query_one("#tree", Tree)
        return tree.cursor_node

    def _toggle_current_host_selection(self) -> None:
        node = self._current_node()
        if not node or not node.data:
            return
        if node.data.get("kind") != "host":
            return
        host: HostEntry = node.data["host"]
        selected = self._toggle_host_selection(host)
        node.set_label(self._format_host_label(host))
        self._show_host_details(host)
        status = "Selected" if selected else "Deselected"
        self._log_event(True, f"{status} {host.alias}")

    def _apply_pane_width(self) -> None:
        try:
            left = self.query_one("#left-pane")
            right = self.query_one("#right-pane")
        except Exception:
            return

        clamp = lambda value, lo, hi: max(lo, min(hi, value))
        self.left_width = clamp(self.left_width, 0.1, 0.85)

        if self.left_collapsed or self.left_width <= 0.05:
            left.styles.display = "none"
            right.styles.width = "1fr"
            return

        left.styles.display = "block"
        left.styles.width = f"{self.left_width*100:.1f}%"
        right.styles.width = f"{(1 - self.left_width)*100:.1f}%"
        self._stored_left_width = self.left_width

    def _apply_search(self, query: str, *, announce: bool, update_field: bool = True) -> None:
        normalized = query
        previous = (self.search_query or "")
        changed = normalized != previous
        if not normalized:
            if self.search_query is None and not announce:
                return
            self.search_query = None
        else:
            self.search_query = normalized
        search_input = None
        try:
            search_input = self.query_one("#search-box", Input)
        except Exception:
            pass
        was_focus = bool(search_input and search_input.has_focus)
        if update_field and search_input and search_input.value != normalized:
            search_input.value = normalized
        self._rebuild_tree()
        if was_focus:
            try:
                refreshed_input = self.query_one("#search-box", Input)
            except Exception:
                refreshed_input = None
            if refreshed_input:
                refreshed_input.focus()
                if hasattr(refreshed_input, "action_end"):
                    refreshed_input.action_end()
        if announce and changed:
            if self.search_query:
                count = self._search_result_count
                plural = "match" if count == 1 else "matches"
                message = (
                    f"Search '{self.search_query}' found {count} {plural}."
                    if count
                    else f"No matches for '{self.search_query}'."
                )
                success = self._search_result_count > 0
                self._log_event(success, message)
            else:
                self._log_event(True, "Cleared search filter.")

    def _clear_search(self, announce: bool = True) -> None:
        if not self.search_query:
            if announce:
                self._log_event(False, "No active search filter to clear.")
            return
        self._apply_search("", announce=announce)

    # ------------------------------------------------------------------- actions
    def action_connect_selected(self) -> None:
        if self._modal_open():
            return
        if self.selected_hosts:
            hosts = self._resolve_selected_hosts()
            if not hosts:
                self._log_event(False, "Selected hosts not available; reload and try again.")
                self.selected_hosts.clear()
                return
            self._launch_hosts(hosts)
            return
        node = self._current_node()
        if not node:
            return
        hosts = self._collect_hosts_from_node(node, hosts_only=True)
        self._launch_hosts(hosts)

    def action_connect_group(self) -> None:
        if self._modal_open():
            return
        node = self._current_node()
        if not node:
            return
        hosts = self._collect_hosts_from_node(node, hosts_only=False)
        self._launch_hosts(hosts)

    def _launch_hosts(self, hosts: List[HostEntry]) -> None:
        if not hosts:
            self._log_event(False, "No hosts available for this selection.")
            return
        for host in hosts:
            opened = self._open_terminal_tab(host)
            if not opened:
                success, message = self.launcher.launch(host)
                self._log_event(success, message)

    def action_add_parent_group(self) -> None:
        if self._modal_open():
            return
        self._open_text_modal(
            action="add_parent",
            title="Add Parent Group",
            prompt="Directory name (no slashes):",
        )

    def action_add_child_group(self) -> None:
        if self._modal_open():
            return
        node = self._current_node()
        if node is None:
            self._log_event(False, "Select a parent group first.")
            return
        data = node.data or {}
        kind = data.get("kind")
        if kind == "parent":
            parent = data["name"]
        elif kind == "child":
            parent = data["parent"]
        elif kind == "host":
            host: HostEntry = data["host"]
            parent = host.parent_group
        else:
            self._log_event(False, "Select a parent or child group first.")
            return
        self._open_text_modal(
            action="add_child",
            title="Add Child Group",
            prompt=f"File name for {parent}:",
            extra={"parent": parent},
        )

    def action_add_host(self) -> None:
        if self._modal_open():
            return
        node = self._current_node()
        if node is None:
            self._log_event(False, "Select a child group to add a host.")
            return
        data = node.data or {}
        kind = data.get("kind")
        if kind == "child":
            parent = data["parent"]
            child = data["name"]
        elif kind == "host":
            host: HostEntry = data["host"]
            parent = host.parent_group
            child = host.child_group
        else:
            self._log_event(False, "Select a child group to add a host.")
            return
        group = self.manager.tree.get(parent, {}).get(child)
        if group is None:
            self._log_event(False, "Child group not found; reload and retry.")
            return
        self._open_host_modal(
            action="add_host",
            title="Add Host",
            parent=parent,
            child=child,
            extra={"parent": parent, "child": child},
        )

    def action_edit_selected(self) -> None:
        if self._modal_open():
            return
        node = self._current_node()
        if node is None:
            self._log_event(False, "Select something to edit.")
            return
        data = node.data or {}
        kind = data.get("kind")
        if kind == "parent":
            name = data["name"]
            self._open_text_modal(
                action="rename_parent",
                title="Rename Parent Group",
                prompt="New directory name:",
                initial=name,
                extra={"name": name},
            )
        elif kind == "child":
            parent = data["parent"]
            name = data["name"]
            self._open_text_modal(
                action="rename_child",
                title="Rename Child Group",
                prompt="New file name:",
                initial=name,
                extra={"parent": parent, "name": name},
            )
        elif kind == "host":
            host: HostEntry = data["host"]
            self._open_host_modal(
                action="edit_host",
                title="Edit Host",
                parent=host.parent_group,
                child=host.child_group,
                initial=host,
                extra={
                    "parent": host.parent_group,
                    "child": host.child_group,
                    "original_alias": host.alias,
                },
            )
        else:
            self._log_event(False, "Nothing to edit here.")

    def action_search(self) -> None:
        if self._modal_open():
            return
        try:
            search_input = self.query_one("#search-box", Input)
        except Exception:
            search_input = None
        if search_input:
            search_input.focus()
            if search_input.value and hasattr(search_input, "action_end"):
                search_input.action_end()

    def action_clear_search(self) -> None:
        if self._modal_open():
            return
        self._clear_search()
        try:
            search_input = self.query_one("#search-box", Input)
        except Exception:
            search_input = None
        if search_input:
            search_input.value = ""
            search_input.focus()

    def action_toggle_left_pane(self) -> None:
        if self._modal_open():
            return
        if not self.left_collapsed:
            self._stored_left_width = self.left_width or self._stored_left_width
        self.left_collapsed = not self.left_collapsed
        if not self.left_collapsed and self.left_width <= 0.05:
            self.left_width = self._stored_left_width or 0.38
        self._apply_pane_width()
        if not self.left_collapsed:
            tree = self.query_one("#tree", Tree)
            tree.focus()

    def _adjust_left_width(self, delta: float) -> None:
        if self.left_collapsed:
            self.left_collapsed = False
            if self.left_width <= 0.05:
                self.left_width = self._stored_left_width or 0.38
        self.left_width += delta
        self._apply_pane_width()

    def action_shrink_left_pane(self) -> None:
        if self._modal_open():
            return
        self._adjust_left_width(-0.05)

    def action_expand_left_pane(self) -> None:
        if self._modal_open():
            return
        self._adjust_left_width(0.05)

    def action_move_selected(self) -> None:
        if self._modal_open():
            return
        if self.selected_hosts:
            host_keys = set(self.selected_hosts)
            self._open_move_hosts_modal(host_keys)
            return
        node = self._current_node()
        if node is None or not node.data:
            self._log_event(False, "Select a subgroup or host to move.")
            return
        data = node.data
        kind = data.get("kind")
        if kind == "host":
            host: HostEntry = data["host"]
            host_keys = {self._host_key(host)}
            self._open_move_hosts_modal(host_keys)
        elif kind == "child":
            parent = data["parent"]
            child = data["name"]
            self._open_move_child_modal(parent, child)
        else:
            self._log_event(False, "Move is only available for subgroups or hosts.")

    def action_delete_selected(self) -> None:
        if self._modal_open():
            return
        node = self._current_node()
        if node is None or not node.data:
            self._log_event(False, "Select something to delete.")
            return
        data = node.data
        kind = data.get("kind")
        if kind == "parent":
            name = data["name"]
            self._open_confirm_modal(
                action="delete_parent",
                title="Remove Parent Group",
                message=(
                    f"Delete parent group [b]{name}[/b] and all nested configs?\n"
                    "This cannot be undone."
                ),
                confirm_label="Delete",
                extra={"name": name},
            )
        elif kind == "child":
            parent = data["parent"]
            name = data["name"]
            self._open_confirm_modal(
                action="delete_child",
                title="Remove Child Group",
                message=(
                    f"Delete config file [b]{parent}/{name}[/b] and its hosts?\n"
                    "This cannot be undone."
                ),
                confirm_label="Delete",
                extra={"parent": parent, "name": name},
            )
        elif kind == "host":
            host: HostEntry = data["host"]
            self._open_confirm_modal(
                action="delete_host",
                title="Remove Host",
                message=(
                    f"Delete host [b]{host.alias}[/b] from {host.parent_group}/{host.child_group}?\n"
                    "This action cannot be undone."
                ),
                confirm_label="Delete",
                extra={
                    "parent": host.parent_group,
                    "child": host.child_group,
                    "alias": host.alias,
                },
            )
        else:
            self._log_event(False, "Nothing to delete here.")

    # ------------------------------------------------------------ button events
    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "btn-add-parent": self.action_add_parent_group,
            "btn-add-child": self.action_add_child_group,
            "btn-add-host": self.action_add_host,
            "btn-edit": self.action_edit_selected,
        }
        handler = mapping.get(event.button.id or "")
        if handler:
            self._invoke_action(handler)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-box":
            self._apply_search(event.value, announce=False, update_field=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-box":
            event.stop()
            self._apply_search(event.value, announce=True)
            self._ensure_tree_focus()

    def on_input_key(self, event: Input.Key) -> None:
        if event.input.id != "search-box":
            return
        key = event.key
        pane_keys = {
            "[": self.action_shrink_left_pane,
            "]": self.action_expand_left_pane,
            "\\": self.action_toggle_left_pane,
        }
        handler = pane_keys.get(key)
        if handler:
            event.stop()
            self._invoke_action(handler)
            return

    # ------------------------------------------------------------------- key map
    def on_key(self, event: Key) -> None:
        if self._modal_open():
            return
        tree = self.query_one("#tree", Tree)

        if event.key == "escape":
            if self.search_query:
                event.stop()
                self._clear_search()
            return

        key = event.character or ""

        if not tree.has_focus:
            pane_keys = {
                "[": self.action_shrink_left_pane,
                "]": self.action_expand_left_pane,
                "\\": self.action_toggle_left_pane,
            }
            handler = pane_keys.get(key)
            if handler:
                event.stop()
                self._invoke_action(handler)
            return

        if key == " ":
            event.stop()
            self._toggle_current_host_selection()
            return

        if event.key == "enter" and getattr(event, "modifiers", None) and "shift" in event.modifiers:
            event.stop()
            self._invoke_action(self.action_connect_group)
            return

        mapping = {
            "a": self.action_add_host,
            "g": self.action_add_child_group,
            "G": self.action_add_parent_group,
            "c": self.action_connect_selected,
            "p": self.action_connect_group,
            "r": self.action_reload,
            "e": self.action_edit_selected,
            "m": self.action_move_selected,
            "d": self.action_delete_selected,
            "/": self.action_search,
            "[": self.action_shrink_left_pane,
            "]": self.action_expand_left_pane,
            "\\": self.action_toggle_left_pane,
            "q": self.action_quit,
        }

        handler = mapping.get(key)
        if handler:
            event.stop()
            self._invoke_action(handler)
            return

        if event.key == "enter":
            event.stop()
            self._invoke_action(self.action_connect_selected)

    # -------------------------------------------------------------- modal logic
    def _handle_text_result(
        self, context: Dict[str, Any], value: Optional[str]
    ) -> None:
        action = context.get("action")
        if action == "add_parent":
            if not value:
                return
            success, text = self.manager.create_parent_group(value)
        elif action == "add_child":
            parent = context.get("parent")
            if not parent or not value:
                return
            success, text = self.manager.create_child_group(parent, value)
        elif action == "rename_parent":
            old = context.get("name")
            if not old or not value or value == old:
                return
            success, text = self.manager.rename_parent_group(old, value)
        elif action == "rename_child":
            parent = context.get("parent")
            old = context.get("name")
            if not parent or not old or not value or value == old:
                return
            success, text = self.manager.rename_child_group(parent, old, value)
        elif action == "search":
            self._apply_search(value or "", announce=True)
            return
        else:
            return

        self._log_event(success, text)
        if success:
            self._rebuild_tree()

    def _handle_host_result(
        self, context: Dict[str, Any], payload: Optional[Dict[str, Any]]
    ) -> None:
        if not payload:
            return
        action = context.get("action")
        if action == "add_host":
            parent = context.get("parent")
            child = context.get("child")
            if not parent or not child:
                self._log_event(False, "Missing group information for new host.")
                return
            group = self.manager.tree.get(parent, {}).get(child)
            if group is None:
                self._log_event(False, "Child group not found; reload and retry.")
                return
            host_entry = HostEntry(
                alias=str(payload["alias"]),
                options=dict(payload["options"]),
                source_file=group.path,
                parent_group=parent,
                child_group=child,
            )
            success, text = self.manager.add_host(parent, child, host_entry)
        elif action == "edit_host":
            parent = context.get("parent")
            child = context.get("child")
            original = context.get("original_alias")
            if not parent or not child or not original:
                self._log_event(False, "Missing context for host edit.")
                return
            group = self.manager.tree.get(parent, {}).get(child)
            if group is None:
                self._log_event(False, "Group not found; reload and retry.")
                return
            updated_host = HostEntry(
                alias=str(payload["alias"]),
                options=dict(payload["options"]),
                source_file=group.path,
                parent_group=parent,
                child_group=child,
            )
            success, text = self.manager.update_host(parent, child, original, updated_host)
        else:
            return

        self._log_event(success, text)
        if success:
            self._rebuild_tree()

    def _handle_confirm_result(
        self, context: Dict[str, Any], confirmed: bool
    ) -> None:
        if not confirmed:
            return
        action = context.get("action")
        if action == "delete_parent":
            name = context.get("name")
            if not name:
                return
            success, text = self.manager.delete_parent_group(name)
            if success:
                self.selected_hosts = {
                    key for key in self.selected_hosts if key[0] != name
                }
        elif action == "delete_child":
            parent = context.get("parent")
            name = context.get("name")
            if not parent or not name:
                return
            success, text = self.manager.delete_child_group(parent, name)
            if success:
                self.selected_hosts = {
                    key for key in self.selected_hosts
                    if not (key[0] == parent and key[1] == name)
                }
        elif action == "delete_host":
            parent = context.get("parent")
            child = context.get("child")
            alias = context.get("alias")
            if not parent or not child or not alias:
                return
            success, text = self.manager.delete_host(parent, child, alias)
            if success:
                self.selected_hosts.discard((parent, child, alias))
        else:
            return

        self._log_event(success, text)
        if success:
            self.selected_host = None
            self._rebuild_tree()

    def _handle_move_hosts(
        self,
        host_keys: Set[Tuple[str, str, str]],
        payload: Optional[Dict[str, Any]],
    ) -> None:
        if not payload:
            return
        target_parent = payload.get("parent")
        target_child = payload.get("child")
        if not target_parent or not target_child:
            return
        success, text = self.manager.move_hosts(
            host_keys,
            target_parent,
            target_child,
        )
        self._log_event(success, text)
        if success:
            self.selected_hosts = {
                (target_parent, target_child, alias)
                for (_, _, alias) in host_keys
            }
            self.selected_host = None
            self._rebuild_tree()

    def _handle_move_child(
        self,
        parent: str,
        child: str,
        host_aliases: List[str],
        payload: Optional[Dict[str, Any]],
    ) -> None:
        if not payload:
            return
        target_parent = payload.get("parent")
        target_child = payload.get("child", child)
        if not target_parent or not target_child:
            return
        success, text = self.manager.move_child_group(
            parent,
            child,
            target_parent,
            target_child_name=target_child,
        )
        self._log_event(success, text)
        if success:
            def remap(key: Tuple[str, str, str]) -> Tuple[str, str, str]:
                p, c, alias = key
                if p == parent and c == child:
                    return (target_parent, target_child, alias)
                return key

            self.selected_hosts = {remap(key) for key in self.selected_hosts}
            if host_aliases:
                for alias in host_aliases:
                    self.selected_hosts.add((target_parent, target_child, alias))
            self.selected_host = None
            self._rebuild_tree()

    # ---------------------------------------------------------- detail rendering
    def _show_host_details(self, host: HostEntry) -> None:
        details = self.query_one("#details", Static)
        table = Table.grid(padding=(0, 1))
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Value", style="magenta")
        table.add_row("Alias", host.alias)
        table.add_row("Group", f"{host.parent_group}/{host.child_group}")
        table.add_row("Source", str(host.source_file))
        table.add_row(
            "Selected",
            "Yes" if self._host_key(host) in self.selected_hosts else "No",
        )
        if self.search_query:
            table.add_row(
                "Matches Search",
                "Yes" if self._host_key(host) in self._matched_hosts else "No",
            )
        for key, value in host.summary_pairs():
            table.add_row(key, value)
        details.update(Panel(table, title=f":electric_plug: {host.display_title()}"))

    def _show_group_details(self, node: TreeNode) -> None:
        details = self.query_one("#details", Static)
        data = node.data or {}
        parent = data.get("parent", "")
        name = data.get("name", "")
        group = self.data.get(parent, {}).get(name)
        hosts = group.hosts if group else []
        table = Table.grid(padding=(0, 1))
        table.add_column("Host", style="green")
        table.add_column("Target", style="magenta")
        for host in hosts:
            target = host_option(host, "hostname") or "(Alias)"
            icon = (
                ":check_mark_button:"
                if self._host_key(host) in self.selected_hosts
                else ":electric_plug:"
            )
            icon = (
                ":mag_right:"
                if self.search_query and self._host_key(host) in self._matched_hosts
                else icon
            )
            alias_text = (
                f"[yellow]{host.alias}[/yellow]"
                if self.search_query and self._host_key(host) in self._matched_hosts
                else host.alias
            )
            table.add_row(f"{icon} {alias_text}", target)
        details.update(
            Panel(
                table if hosts else "No hosts in this group.",
                title=f":open_file_folder: {parent}/{name}",
                subtitle=f"{len(hosts)} host(s)"
                + (f" • {group.path}" if group else ""),
            )
        )

    def _show_default_details(self) -> None:
        details = self.query_one("#details", Static)
        text = (
            ":open_file_folder: parent groups • :page_facing_up: child configs • :electric_plug: hosts • :check_mark_button: selected\n"
            "Use ↑↓ to pick a host.\n"
            "Space toggle host selection • Enter/c connect selection or focused host • Shift+Enter/p connect group.\n"
            "r reload • g add child • G add parent • a add host • e edit • d delete • m move • / search • Esc clear search.\n"
            "[yellow][[ ] shrink • ]] expand • \\ toggle tree pane[/yellow]"
        )
        if self.search_query:
            matches = self._search_result_count
            match_label = "match" if matches == 1 else "matches"
            text += (
                f"\n[cyan]Search[/cyan]: '{self.search_query}' • {matches} {match_label}."
                "\nPress Esc to clear the search filter."
            )
        details.update(text)

    # ------------------------------------------------------- helper enumeration
    def _collect_hosts_from_node(
        self, node: TreeNode, hosts_only: bool
    ) -> List[HostEntry]:
        data = node.data or {}
        kind = data.get("kind")
        if kind == "host":
            return [data["host"]]
        if kind == "child":
            parent = data["parent"]
            child_name = data["name"]
            group = self.data.get(parent, {}).get(child_name)
            return list(group.hosts) if group else []
        if kind == "parent":
            hosts: List[HostEntry] = []
            for group in self.data.get(data["name"], {}).values():
                hosts.extend(group.hosts)
            return hosts
        if not hosts_only:
            hosts = []
            for groups in self.data.values():
                for group in groups.values():
                    hosts.extend(group.hosts)
            return hosts
        return []

    def on_terminal_widget_terminal_closed(
        self, event: TerminalWidget.TerminalClosed
    ) -> None:
        pane_id = event.pane_id
        tabs = self._terminal_tabs()
        try:
            tabs.remove_pane(pane_id)
        except Exception:
            pass
        self.terminal_tabs_map.pop(pane_id, None)
        self._ensure_terminal_placeholder()
        self._main_tabs().active = "terminals"
        title = event.title
        self._log_event(False, f"Terminal session closed for {title}.")
