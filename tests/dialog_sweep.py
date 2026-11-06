"""Build every dialog class with long messages and measure its width against its declared DialogSize.

Used by tests/test_dialog_sweep.py; ``python3 -m tests.dialog_sweep`` prints the table (needs a display).
A non-resizable dialog opens at its natural width, a resizable one at max(default, minimum) width; every
wrapping label gets a 400-character text first (the bug class: a wrapping label without width_chars /
max_width_chars asks for its unwrapped width).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Tuple
from unittest import mock

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

LONG = " ".join(["a-long-message-word"] * 21)  # about 400 characters
TOLERANCE = 1.08


def factories(win, tmp: Path) -> Dict[str, Callable[[], Gtk.Dialog]]:
    from ophion.auth_flow import PasswordRequest, SaveContext, SaveOffer
    from ophion.gtkui.dialogs import (bulk_tag, child_group_chooser, config_viewer, confirm, credentials,
                                      credentials_transfer, group_picker, host_key, known_hosts, multirun_confirm,
                                      multirun_targets, native_export, native_import, openssh_import, password,
                                      prompt, rdp_diagnostics, secrets, ssh_keys, trust, tunnels)
    from ophion.gtkui.dialogs import mobaxterm_import
    from ophion.gtkui.unlock_flow import RecoveryKeyDialog
    from ophion.multirun.engine import Target

    config = tmp / "config"
    config.write_text("Host a\n    HostName a.example\n")
    request = PasswordRequest(alias="Develop - 192.168.112.23", target="ftofighi@192.168.112.23:22", attempt=1,
                              rejected="saved password (credential ‘ftofighi’)")
    change = host_key.ChangedKey(host_label="web-01", names=[("web-01.example.internal", 22)],
                                 new_fingerprint="SHA256:" + "x" * 43, key_type="ssh-ed25519",
                                 known_hosts_path=str(tmp / "known_hosts"), line_no=3,
                                 old_fingerprints=("SHA256:" + "y" * 43,))
    return {
        "PasswordDialog": lambda: password.PasswordDialog(),
        "PasswordRequestDialog": lambda: password.PasswordRequestDialog(win, request, lambda _p: None),
        "SavePasswordDialog": lambda: password.SavePasswordDialog(
            win, SaveOffer(alias="web-01", verified=True, context=SaveContext()), lambda _d: None),
        "RecoveryKeyDialog": lambda: RecoveryKeyDialog(win, "ABCD-EFGH-IJKL-MNOP-QRST-UVWX"),
        "CredentialEditDialog": lambda: credentials.CredentialEditDialog(win),
        "TrustDialog": lambda: trust.TrustDialog(win, "Trust?", LONG, [("Fingerprint", LONG)], accept_label="_Trust",
                                                 dangerous=False),
        "KnownHostsDialog": lambda: known_hosts.KnownHostsDialog(win, path=tmp / "known_hosts"),
        "ConfigViewerDialog": lambda: config_viewer.ConfigViewerDialog(win, config),
        "ForwardDialog": lambda: tunnels.ForwardDialog(win),
        "HostPickerDialog": lambda: tunnels.HostPickerDialog(win, [("id-1", "web-01 (10.0.0.1)")]),
        "TunnelsDialog": lambda: tunnels.TunnelsDialog(win, win.tunnels),
        "RdpDiagnosticsDialog": lambda: rdp_diagnostics.RdpDiagnosticsDialog(win, discover=lambda *_a, **_k: None),
        "UnlockDialog": lambda: secrets.UnlockDialog(win, "Unlock", "Unlock Ophion", LONG, lambda _p: True,
                                                     icon_name="sshm-key"),
        "NewSecretDialog": lambda: secrets.NewSecretDialog(win, "Master password", "Create a master password", LONG),
        "ConfirmDialog": lambda: confirm.ConfirmDialog(win, "Delete?", LONG),
        "MessageDialog": lambda: confirm.MessageDialog(win, "Done", LONG),
        "NativeExportDialog": lambda: native_export.NativeExportDialog(win, group_label="prod", host_count=3),
        "OpenSSHImportDialog": lambda: openssh_import.OpenSSHImportDialog(win, home=tmp),
        "NativeImportDialog": lambda: native_import.NativeImportDialog(win, win.manager, win._credential_store,
                                                                       report_dir=tmp / "reports"),
        "MobaXtermImportDialog": lambda: mobaxterm_import.MobaXtermImportDialog(
            win, win.manager, win._credential_store, report_dir=tmp / "reports"),
        "ChildGroupChooserDialog": lambda: child_group_chooser.ChildGroupChooserDialog(win, "Choose", LONG, "prod",
                                                                                       ["web.conf", "db"]),
        "PassphraseDialog": lambda: credentials_transfer.PassphraseDialog(win, "Passphrase", confirm=False,
                                                                          message=LONG),
        "CredentialImportDialog": lambda: credentials_transfer.CredentialImportDialog(win, []),
        "PromptDialog": lambda: prompt.PromptDialog(win, "Rename", LONG),
        "TargetChooserDialog": lambda: multirun_targets.TargetChooserDialog(win, win.manager.tree),
        "RunConfirmDialog": lambda: multirun_confirm.RunConfirmDialog(
            win, [Target(key="k", alias="web-01", hostname="10.0.0.1")], "uptime"),
        "GenerateKeyDialog": lambda: ssh_keys.GenerateKeyDialog(win, win.ssh_keys),
        "SshKeysDialog": lambda: ssh_keys.SshKeysDialog(win, win.ssh_keys),
        "BulkTagDialog": lambda: bulk_tag.BulkTagDialog(win, 3),
        "HostKeyDetailsDialog": lambda: host_key.HostKeyDetailsDialog(win, change),
        "GroupPickerDialog": lambda: group_picker.GroupPickerDialog(win, win.manager, "Choose a group"),
        "HostDialog": lambda: win.hosts.host_dialog(),
        "MultiRunDialog": lambda: _multirun(win),
        "PreferencesDialog": lambda: _preferences(win),
    }


def _multirun(win):
    from ophion.gtkui.dialogs.multirun import MultiRunDialog

    return MultiRunDialog(win, win.multirun)


def _preferences(win):
    from ophion.gtkui.dialogs.preferences import PreferencesDialog

    made = []
    with mock.patch.object(PreferencesDialog, "run", lambda dialog: made.append(dialog) or Gtk.ResponseType.CANCEL):
        with mock.patch.object(PreferencesDialog, "destroy", lambda _dialog: None):
            win.app_dialogs.open_settings()
    return made[0]


def labels_of(widget) -> List[Gtk.Label]:
    found: List[Gtk.Label] = []

    def walk(item):
        if isinstance(item, Gtk.Label):
            found.append(item)
        if isinstance(item, Gtk.Container):
            item.forall(walk)
    walk(widget)
    return found


def measure(dialog, long_text: bool = True, fitted: bool = True) -> Tuple[int, int, bool]:
    """``(declared width, width it opens at, resizable)``; ``long_text``: every wrapping label gets LONG."""

    from ophion.gtkui.dialogs.base import DialogSize, dialog_width

    dialog.get_content_area().show_all()
    for label in labels_of(dialog.get_content_area()) if long_text else ():
        if label.get_line_wrap():
            label.set_text(LONG)
    if fitted and hasattr(dialog, "fit_wrapping_labels"):
        dialog.fit_wrapping_labels()  # what BaseDialog does before it shows
    nominal = dialog_width(getattr(dialog, "size_class", DialogSize.MEDIUM))
    minimum, natural = dialog.get_preferred_width()
    resizable = dialog.get_resizable()
    opens = max(minimum, dialog.get_default_size()[0]) if resizable else natural
    return nominal, opens, resizable


def sweep(win, long_text: bool = True, fitted: bool = True) -> List[Tuple[str, int, int, bool, str]]:
    """``(class, declared, opens at, resizable, problem)`` for every dialog (problem "" = within 8 %)."""

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, make in factories(win, Path(tmp)).items():
            try:
                dialog = make()
            except Exception as exc:  # pragma: no cover - reported in the table
                rows.append((name, 0, 0, False, f"not built: {type(exc).__name__}: {exc}"))
                continue
            try:
                nominal, opens, resizable = measure(dialog, long_text, fitted)
            finally:
                dialog.destroy()
            problem = "too wide" if opens > nominal * TOLERANCE else ""
            rows.append((name, nominal, opens, resizable, problem))
    return rows


def table(rows) -> str:
    lines = [f"| {'dialog':26} | {'declared':>8} | {'opens at':>8} | resizable | result |", "|" + "-" * 70 + "|"]
    for name, nominal, opens, resizable, problem in rows:
        lines.append(f"| {name:26} | {nominal:8} | {opens:8} | {str(resizable):9} | {problem or 'ok'} |")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    from ophion.gtkui.theme import density
    from tests.gtk_window_fixture import WindowSandbox

    sandbox = WindowSandbox(store=True)
    try:
        for preset in (density.Density.COMFORTABLE, density.Density.COMPACT):
            density.set_active(preset)
            for long_text in (False, True):
                for fitted in (False, True):
                    print(preset.value, "long messages" if long_text else "as shipped",
                          "after" if fitted else "before")
                    print(table(sweep(sandbox.window, long_text, fitted)))
    finally:
        sandbox.close()
