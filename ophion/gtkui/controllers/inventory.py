"""Configuration reloads, shared read-only inventories and Git update checks."""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib  # type: ignore  # noqa: E402

from ...config import ConfigManager  # noqa: E402
from ..dialogs.confirm import show_message  # noqa: E402
from ..dialogs.files import choose_folder, choose_open_file  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from ..workspaces import Workspace, remember_workspace  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


class InventoryController:
    """Configuration reloads, shared read-only inventories and Git update checks."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.shared_manager: Optional[ConfigManager] = None
        self.shared_path: Optional[Path] = None
        self.shared_poll_id = 0
        self.shared_mtime: Optional[float] = None
        self.shared_missing_warned = False
        self.git_watch_thread: Optional[threading.Thread] = None
        self.git_watch_stop = threading.Event()
        self.git_updates_available = False
        self.git_watch_path: Optional[Path] = None

    def refresh(self) -> None:
        self.win.tree.rebuild(refresh=True)

    def open_inventory_folder(self) -> None:
        """SSH config folders are import sources for the app inventory (never its live store)."""

        if getattr(self.win.manager, "store", None) is not None:
            self.win.store_actions.import_openssh()
            return
        folder = choose_folder(self.win, _("Open SSH Config Folder"), current_folder=str(Path.home() / ".ssh"))
        if not folder:
            return
        self.load_workspace(Path(folder))

    def load_workspace(self, path: Path) -> None:
        """Switch the main inventory to ``path`` and remember the previous one."""

        ui = self.win._ui
        ui.recent_inventories = remember_workspace(ui.recent_inventories, self.win.manager.base_dir)
        self.win.manager = self._manager_for(Path(path))
        self.win.colors.activate()  # each inventory has its own tree colours
        ui.recent_inventories = remember_workspace(ui.recent_inventories, self.win.manager.base_dir)
        self.win.refresh_workspaces()
        self.win.tree.rebuild(refresh=False)
        self.win._save_ui_state()
        if self.win.manager.read_only:
            self.win._notify_info(_("Opened {path} read-only: it is a single file. Changes go to a folder; you will "
                                    "be asked which one.").format(path=path))
        else:
            self.win._notify_info(_("Loaded configuration from {path}.").format(path=path))

    def _manager_for(self, path: Path):
        """The app store for an inventory database (``*.db``), else a folder/file inventory."""

        if path.suffix == ".db":
            from ...inventory.manager import StoreConfigManager
            from ...inventory.startup import Choice, empty_read_only_store, open_inventory_store

            store = open_inventory_store(path, lambda _kind, _message: Choice.READ_ONLY)
            return StoreConfigManager(store or empty_read_only_store(_("The inventory could not be opened.")))
        return ConfigManager(path)

    def switch_workspace(self, workspace: Workspace) -> None:
        if workspace.read_only:
            return
        if not workspace.path.exists():
            self.win._notify(_("Workspace folder not found: {path}").format(path=workspace.path), Level.WARNING)
            return
        self.load_workspace(workspace.path)

    def set_git_updates(self, available: bool) -> None:
        newly_available = available and not self.git_updates_available
        self.git_updates_available = available
        self.win._header.set_git_updates(available)
        if newly_available:
            self.win._notify(
                _("New commits are available in the tracked Git repository."),
                Level.INFO,
                action_label=_("_Details"),
                action=self.show_git_updates,
            )

    def check_git_now(self) -> None:
        if self.git_updates_available:
            self.show_git_updates()
            return
        if not self.git_watch_path:
            self.win._notify(
                _("Git update checks are off. Enable them under Settings → Git Sync."),
                Level.INFO,
                action_label=_("Open _Settings"),
                action=self.win.app_dialogs.open_settings,
            )
            return

        def worker() -> None:
            self.check_git_repo_for_updates()
            GLib.idle_add(self.report_git_check)

        threading.Thread(target=worker, daemon=True).start()
        self.win._notify_info(_("Checking {path} for updates…").format(path=self.git_watch_path))

    def report_git_check(self) -> bool:
        if not self.git_updates_available:
            self.win._notify(_("The tracked repository is up to date."), Level.SUCCESS)
        return GLib.SOURCE_REMOVE

    def git_watch_loop(self, interval: int) -> None:
        while not self.git_watch_stop.is_set():
            self.check_git_repo_for_updates()
            self.git_watch_stop.wait(interval)

    def stop_git_watch(self) -> None:
        if self.git_watch_thread and self.git_watch_thread.is_alive():
            self.git_watch_stop.set()
            self.git_watch_thread.join(timeout=1.0)
        self.git_watch_thread = None
        self.git_watch_stop.clear()

    def refresh_git_watch(self) -> None:
        self.stop_git_watch()
        self.set_git_updates(False)
        if not getattr(self.win._settings, "git_watch_enabled", False):
            return
        watch_target = self.win._settings.git_watch_path or (str(self.shared_path) if self.shared_path else "")
        if not watch_target:
            return
        path = Path(watch_target).expanduser()
        if not (path / ".git").exists():
            return
        self.git_watch_path = path
        interval = max(60, int(getattr(self.win._settings, "git_watch_interval", 600) or 600))
        self.git_watch_stop.clear()
        self.git_watch_thread = threading.Thread(target=self.git_watch_loop, args=(interval,), daemon=True)
        self.git_watch_thread.start()

    def check_git_repo_for_updates(self) -> None:
        path = self.git_watch_path
        if not path or not (path / ".git").exists():
            return
        devnull = subprocess.DEVNULL
        try:
            subprocess.run(
                ["git", "-C", str(path), "fetch", "--all", "--prune"],
                stdout=devnull,
                stderr=devnull,
                check=False,
                timeout=30,
            )
        except Exception:
            return
        try:
            head = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=devnull,
                check=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            upstream = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "@{u}"],
                stdout=subprocess.PIPE,
                stderr=devnull,
                check=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            behind = subprocess.run(
                ["git", "-C", str(path), "rev-list", "--count", f"{head}..{upstream}"],
                stdout=subprocess.PIPE,
                stderr=devnull,
                check=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            available = behind.isdigit() and int(behind) > 0
        except Exception:
            return
        GLib.idle_add(self.set_git_updates, available, priority=GLib.PRIORITY_DEFAULT)

    def show_git_updates(self) -> None:
        path_text = str(self.git_watch_path) if self.git_watch_path else ""
        message = "Remote updates are available. Run 'git pull' in the tracked directory to refresh."
        if path_text:
            message += f"\nDirectory: {path_text}"
        show_message(self.win, _("Git updates available"), message, level=Level.INFO)

    def start_shared_poll(self) -> None:
        if self.shared_poll_id:
            GLib.source_remove(self.shared_poll_id)
            self.shared_poll_id = 0
        if not self.shared_path:
            return
        self.shared_poll_id = GLib.timeout_add_seconds(5, self.check_shared_inventory)

    def check_shared_inventory(self) -> bool:
        if not self.shared_path:
            return False
        try:
            stat_result = self.shared_path.stat()
        except FileNotFoundError:
            if not self.shared_missing_warned:
                self.win._notify(_("Shared inventory file not found."), Level.WARNING)
                self.shared_missing_warned = True
            self.shared_mtime = None
            return True
        except OSError as exc:
            if not self.shared_missing_warned:
                self.win._notify(_("Unable to read shared inventory: {error}").format(error=exc), Level.ERROR)
                self.shared_missing_warned = True
            return True

        self.shared_missing_warned = False
        mtime = stat_result.st_mtime
        if self.shared_mtime is not None and mtime <= self.shared_mtime:
            return True

        try:
            manager = ConfigManager(self.shared_path)
        except Exception as exc:
            self.win._notify(_("Failed to reload shared inventory: {error}").format(error=exc), Level.ERROR)
            return True
        if not manager.tree:
            self.win._notify(_("Failed to reload shared inventory — invalid format."), Level.ERROR)
            return True

        self.shared_manager = manager
        self.shared_mtime = mtime
        self.win._notify(_("Reloaded shared inventory."), Level.SUCCESS)
        self.win.tree.rebuild(refresh=False)
        return True

    def open_shared_inventory(self) -> None:
        filename = choose_open_file(self.win, _("Open Shared Inventory"))
        if not filename:
            return
        path = Path(filename)
        manager = ConfigManager(path)
        if not manager.tree:
            self.win._notify(_("Unable to load shared inventory."), Level.ERROR)
            return
        self.shared_manager = manager
        self.shared_path = path
        try:
            self.shared_mtime = path.stat().st_mtime
        except OSError:
            self.shared_mtime = None
        self.shared_missing_warned = False
        self.win._notify(_("Loaded shared inventory from {path}.").format(path=path), Level.SUCCESS)
        self.win.tree.rebuild(refresh=True)
        self.win.refresh_workspaces()
        self.start_shared_poll()
        self.refresh_git_watch()

    def resolve_config_view_path(self) -> Path:
        # Prefer explicit config file if base_dir is a file
        if self.win.manager.base_dir.is_file():
            return self.win.manager.base_dir
        candidates = [
            Path("~/.ssh/config").expanduser(),
            self.win.manager.base_dir / "config",
        ]
        for cand in candidates:
            if cand.exists() and cand.is_file():
                return cand
        # Fallback to default even if missing (viewer will show message)
        return candidates[0]

    def shutdown(self) -> None:
        """Stop polling the shared inventory and the Git watcher (window destroy)."""

        if self.shared_poll_id:
            GLib.source_remove(self.shared_poll_id)
            self.shared_poll_id = 0
        self.stop_git_watch()
