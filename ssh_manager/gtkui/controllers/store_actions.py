"""App-store actions: OpenSSH import/export, backups, first-run import offer."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ...inventory.openssh import (  # noqa: E402
    DEFAULT_EXPORT,
    Policy,
    apply_include_line,
    commit_openssh_import,
    import_sources_found,
    include_line_change,
    plan_openssh_import,
    write_export,
)
from ...inventory.store import InventoryError  # noqa: E402
from ...secure_store import SecureStoreError  # noqa: E402
from ..dialogs.confirm import confirm, show_message  # noqa: E402
from ..dialogs.files import choose_save_file  # noqa: E402
from ..dialogs.openssh_import import OpenSSHImportDialog  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)
FIRST_RUN_META = "first_run_import_offered"


def _home(path: Path) -> str:
    try:
        return "~/" + str(Path(path).expanduser().relative_to(Path.home()))
    except ValueError:
        return str(path)


class StoreActions:
    """Actions that only make sense for the app-owned inventory (``StoreConfigManager``)."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win

    @property
    def manager(self):
        manager = self.win.manager
        return manager if getattr(manager, "store", None) is not None else None

    def _require_store(self) -> bool:
        if self.manager is None:
            self.win._notify_info(_("Imports go into the SSH Manager inventory; this window shows a folder inventory."))
            return False
        if self.manager.read_only:
            show_message(self.win, _("The inventory is read-only"), self.manager.read_only_reason, level=Level.WARNING)
            return False
        return True

    # --------------------------------------------------------------- import
    def import_openssh(self, initial: Optional[Path] = None, *, run_dialog: Optional[Callable] = None) -> None:
        if not self._require_store():
            return
        dialog = OpenSSHImportDialog(self.win, initial=initial)
        try:
            accepted = (run_dialog or (lambda d: d.run()))(dialog) == Gtk.ResponseType.OK
            plan, policy = dialog.plan(), dialog.policy()
        finally:
            dialog.destroy()
        if not accepted or plan is None or not plan.host_count:
            return
        self._commit(plan, policy, legacy=True)

    def _commit(self, plan, policy: Policy, *, legacy: bool) -> Optional[object]:
        legacy_bindings = self.win._credential_store.legacy_bindings() if legacy else None
        try:
            report = commit_openssh_import(plan, self.manager, self.win._credential_store, policy=policy,
                                           legacy_bindings=legacy_bindings)
        except (InventoryError, SecureStoreError, KeyError) as exc:
            self.win._notify_error(_("Import failed, no changes were made: {error}").format(error=exc))
            return None
        LOGGER.info("OpenSSH import: %d added, %d skipped", report.added, report.skipped_existing)
        self.win.tree.rebuild(refresh=False)
        if report.first_group:
            self.win.tree.select_parent_group(report.first_group)
        level = Level.WARNING if report.errors or report.bindings_unmatched else Level.SUCCESS
        self.win._notify(report.summary(), level)
        return report

    # ------------------------------------------------------- first-run offer
    def offer_first_run_import(self, *, ask: Optional[Callable[[str, str], bool]] = None, home: Optional[Path] = None) -> bool:
        """Once per source: "Found existing SSH hosts … Import them?" (No changes nothing, never asks again)."""

        manager = self.manager
        if manager is None or manager.read_only:
            return False
        offered = set(filter(None, (manager.store.get_meta(FIRST_RUN_META) or "").split("\n")))
        found = [(path, count) for path, count in import_sources_found(home) if str(path) not in offered]
        if not found:
            return False
        listing = _(" and ").join(
            ngettext("{path} ({n} host)", "{path} ({n} hosts)", count).format(path=_home(path), n=count)
            for path, count in found)
        ask = ask or (lambda title, text: confirm(self.win, title, text, confirm_label=_("_Import")))
        accepted = ask(_("Import existing SSH hosts?"),
                       _("Found existing SSH hosts in {sources}. Import them into SSH Manager? Your files are not "
                         "changed. You can also import later from the menu.").format(sources=listing))
        try:
            manager.store.set_meta(FIRST_RUN_META, "\n".join(sorted(offered | {str(p) for p, _c in found})))
        except InventoryError:
            pass
        if not accepted:
            return False
        folders = [p for p, _c in found if p.is_dir()]
        files = [p for p, _c in found if not p.is_dir()]
        plan = plan_openssh_import(folders)
        if files:
            extra = plan_openssh_import(files, exclude_under=folders)
            plan.hosts += extra.hosts
            plan.groups += [g for g in extra.groups if g not in plan.groups]
            plan.notes += extra.notes
        return self._commit(plan, Policy.SKIP, legacy=True) is not None

    # --------------------------------------------------------------- export
    def export_openssh(self, *, target: Optional[Path] = None, offer_include: bool = True,
                       ask: Optional[Callable[[str, str], bool]] = None) -> Optional[Path]:
        if target is None:
            chosen = choose_save_file(self.win, _("Export to OpenSSH Config"),
                                      current_file=str(DEFAULT_EXPORT.expanduser()), accept_label=_("_Export"))
            if not chosen:
                return None
            target = Path(chosen)
        try:
            Path(target).expanduser().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = write_export(self.win.manager.tree, Path(target))
        except (OSError, ValueError) as exc:
            self.win._notify_error(_("Export failed: {error}").format(error=exc))
            return None
        self.win._notify(_("Exported to {path}.").format(path=_home(path)), Level.SUCCESS)
        if offer_include:
            self.offer_include(path, ask=ask)
        return path

    def offer_include(self, target: Path, *, ask: Optional[Callable[[str, str], bool]] = None) -> Optional[Path]:
        config = Path("~/.ssh/config").expanduser()
        try:
            new, diff = include_line_change(config, target)
        except OSError as exc:
            self.win._notify_error(_("Cannot read {path}: {error}").format(path=_home(config), error=exc))
            return None
        if new is None:
            return None  # already included
        ask = ask or (lambda title, text: confirm(self.win, title, text, confirm_label=_("_Add Include Line")))
        if not ask(_("Add an Include line to ~/.ssh/config?"),
                   _("Then plain ssh and other tools see the exported hosts. A backup is made first; this change "
                     "is added at the top:\n\n{diff}").format(diff=diff)):
            return None
        try:
            backup = apply_include_line(config, target)
        except OSError as exc:
            self.win._notify_error(_("Could not change {path}: {error}").format(path=_home(config), error=exc))
            return None
        message = _("Added the Include line to ~/.ssh/config.")
        if backup is not None:
            message += " " + _("Backup: {path}").format(path=_home(backup))
        self.win._notify(message, Level.SUCCESS)
        return backup

    # -------------------------------------------------------------- backups
    def backup_now(self) -> Optional[Path]:
        manager = self.manager
        if manager is None:
            return None
        try:
            path = manager.store.backup("manual")
        except (OSError, InventoryError) as exc:
            self.win._notify_error(_("Backup failed: {error}").format(error=exc))
            return None
        if path is not None:
            self.win._notify(_("Backed up to {path}.").format(path=_home(path)), Level.SUCCESS)
        return path

    def reveal_inventory(self) -> None:
        manager = self.manager
        if manager is None:
            return
        try:
            Gtk.show_uri_on_window(self.win, manager.base_dir.parent.as_uri(), Gtk.get_current_event_time())
        except GLib.Error as exc:
            self.win._notify_error(_("Cannot open the folder: {error}").format(error=exc.message))
