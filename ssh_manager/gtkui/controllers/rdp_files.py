"""``.rdp`` files in and out of the inventory, and "Wake and Connect" for RDP hosts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ... import protocols  # noqa: E402
from ...config import HostEntry  # noqa: E402
from ...host_utils import host_connection_params  # noqa: E402
from ...importers.mobaxterm import unique_name  # noqa: E402
from ...rdp.rdpfile import RdpFileError, encode_rdp, export_rdp, import_rdp  # noqa: E402
from ...rdp.settings import RdpSettings  # noqa: E402
from ...rdp.wol import wake  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level  # noqa: E402

if TYPE_CHECKING:
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)
MAX_FILES = 200


class RdpFileActions:
    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win

    # ------------------------------------------------------------ import
    def choose_files(self) -> List[str]:
        chooser = Gtk.FileChooserNative.new(_("Import .rdp Files"), self.win, Gtk.FileChooserAction.OPEN,
                                            _("_Import"), _("_Cancel"))
        chooser.set_select_multiple(True)
        rdp_filter = Gtk.FileFilter()
        rdp_filter.set_name(_("Remote Desktop files (*.rdp)"))
        rdp_filter.add_pattern("*.rdp")
        rdp_filter.add_pattern("*.RDP")
        chooser.add_filter(rdp_filter)
        try:
            return list(chooser.get_filenames()) if chooser.run() == Gtk.ResponseType.ACCEPT else []
        finally:
            chooser.destroy()

    def import_files(self, paths: Optional[Sequence[str]] = None) -> List[str]:
        """Import ``.rdp`` files as RDP hosts into a chosen group; returns the report lines."""

        manager = self.win.manager
        if getattr(manager, "read_only", False) or not getattr(manager, "allows_display_names", False):
            self.win._notify(_("RDP hosts can only be added to the SSH Manager inventory."), Level.INFO)
            return []
        paths = list(paths) if paths is not None else self.choose_files()
        if not paths:
            return []
        selection = self.win.hosts.pick_group(_("Choose a Group for the Imported Hosts"), allow_new_child=True,
                                              accept_label=_("_Import"))
        if not selection:
            return []
        parent, child, create_child = selection
        if create_child:
            ok, message = manager.create_child_group(parent, child)
            if not ok:
                self.win._notify(message, Level.ERROR)
                return []
        group = manager.tree.get(parent, {}).get(child)
        taken = {h.alias.casefold() for h in (group.hosts if group else [])}
        report: List[str] = []
        added = 0
        for path in paths[:MAX_FILES]:
            name = Path(path).name
            try:
                data = Path(path).read_bytes()
                result = import_rdp(data, name)
            except (OSError, RdpFileError) as exc:
                report.append(f"{name}: " + (_(str(exc)) if isinstance(exc, RdpFileError) else exc.strerror or ""))
                continue
            alias = unique_name(result.alias, taken)
            taken.add(alias.casefold())
            metadata = {"rdp": result.settings.to_json(), "imported_from": "rdp-file"}
            host = HostEntry(alias=alias, options=dict(result.options), metadata=metadata, parent_group=parent,
                             child_group=child, protocol="rdp")
            ok, message = manager.add_host(parent, child, host)
            if not ok:
                report.append(f"{name}: {message}")
                continue
            added += 1
            report.append(f"{name} → {alias}")
            report += [f"    {_(note)}" for note in result.notes]
        if len(paths) > MAX_FILES:
            report.append(_("Only the first {n} files were imported.").format(n=MAX_FILES))
        self.win.tree.rebuild(refresh=True)
        self.win._notify(ngettext("Imported {n} RDP host.", "Imported {n} RDP hosts.", added).format(n=added),
                         Level.SUCCESS if added else Level.WARNING)
        for line in report:
            self.win._log_event(line, True)
        return report

    # ------------------------------------------------------------ export
    def export_selected(self, path: Optional[str] = None) -> Optional[str]:
        host = self.win.tree.selected_host()
        if host is None or protocols.protocol_of(host) != "rdp":
            self.win._notify(_("Select an RDP host to export it as a .rdp file."), Level.INFO)
            return None
        if path is None:
            chooser = Gtk.FileChooserNative.new(_("Export as .rdp File"), self.win, Gtk.FileChooserAction.SAVE,
                                                _("_Export"), _("_Cancel"))
            chooser.set_do_overwrite_confirmation(True)
            chooser.set_current_name("".join(ch for ch in host.alias if ch not in "/\\:") + ".rdp")
            try:
                if chooser.run() != Gtk.ResponseType.ACCEPT:
                    return None
                path = chooser.get_filename()
            finally:
                chooser.destroy()
        if not path:
            return None
        hostname, port, user = host_connection_params(self.win.creds.host_with_resolved_identity(host))
        settings, _warnings = RdpSettings.for_host(host)
        try:
            Path(path).write_bytes(encode_rdp(export_rdp(hostname, port, user, settings)))
        except OSError as exc:
            self.win._notify(_("The file could not be written: {error}").format(error=exc.strerror), Level.ERROR)
            return None
        self.win._notify(_("Exported “{alias}” (no password is stored in the file).").format(alias=host.alias),
                         Level.SUCCESS)
        return path

    # ------------------------------------------------------- wake on LAN
    def wake_and_connect(self, host: Optional[HostEntry] = None) -> bool:
        host = host or self.win.tree.selected_host()
        if host is None or protocols.protocol_of(host) != "rdp":
            return False
        settings, _warnings = RdpSettings.for_host(host)
        if not settings.wol_mac:
            self.win._notify(_("Set the MAC address for Wake-on-LAN in the host dialog first."), Level.INFO)
            return False
        try:
            wake(settings.wol_mac, settings.wol_broadcast)
        except (ValueError, OSError) as exc:
            self.win._notify(_("The wake-up packet could not be sent: {error}").format(
                error=_(str(exc)) if isinstance(exc, ValueError) else exc.strerror), Level.ERROR)
            return False
        self.win._notify(_("Wake-up packet sent to “{alias}”; connecting in {n} s.").format(
            alias=host.alias, n=settings.wol_wait_s), Level.INFO)
        GLib.timeout_add_seconds(max(0, settings.wol_wait_s), self._connect_after_wake, host)
        return True

    def _connect_after_wake(self, host: HostEntry) -> bool:
        self.win.sessions.open_hosts([host])
        return False


__all__ = ["RdpFileActions"]
