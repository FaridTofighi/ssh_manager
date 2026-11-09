#!/usr/bin/env python3
"""Cross-version data (R9): write a full data folder with one build, read it with another.

    PYTHONPATH=<tree> python3 tools/crossversion_data.py create DIR
    PYTHONPATH=<tree> python3 tools/crossversion_data.py read DIR      # normalised JSON on stdout

``create`` writes, through the app's own APIs (no GUI): an inventory database with groups and hosts,
``settings.json``, ``.ui_state.json`` with colours and favorites, ``smart_groups.json``, a keybox with a master
password, ``passwords.json``, ``macros.enc`` and a plain and an encrypted ``.ophion`` export. ``read`` loads all
of it and prints what it found. ``tools/refactor_crossversion.sh`` runs this with the pre-refactor tree and the
current one in both directions. HOME and the XDG folders point into DIR, so nothing else is read or written.
"""

import json
import os
import sys
from pathlib import Path

MASTER = "crossversion-master"
PASSPHRASE = "crossversion-export"
SECRETS = {("web.internal", 22, "deploy"): "pw-web", ("credential:ops", 0, ""): "pw-ops"}


def _isolate(folder: Path) -> None:
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "run")):
        path = folder / sub
        path.mkdir(parents=True, exist_ok=True)
        if name == "XDG_RUNTIME_DIR":
            path.chmod(0o700)
        os.environ[name] = str(path)


def _paths(folder: Path) -> dict:
    config = folder / "config" / "ophion"
    return {"config": config, "db": folder / "data" / "ophion" / "inventory.db", "export": folder / "export.ophion",
            "encrypted": folder / "export-encrypted.ophion"}


def create(folder: Path) -> None:
    from ophion import native_format as F
    from ophion.exporters.native import build_export
    from ophion.gtkui.controllers.ui_state import UiState, save_ui_state
    from ophion.inventory.store import InventoryStore, NewHost
    from ophion.keybox import Keybox
    from ophion.macros import Macro, MacroStore
    from ophion.secure_store import SecureStore
    from ophion.settings import AppSettings
    from ophion.smart_groups import SmartGroup, save_smart_groups

    paths = _paths(folder)
    paths["config"].mkdir(parents=True, exist_ok=True)
    store = InventoryStore(paths["db"])
    try:
        prod = store.create_group(None, "Prod", note="production")
        dbs = store.create_group(prod.id, "Databases")
        store.create_group(None, "Empty")
        web = store.add_host(NewHost(prod.id, "web-01", "web.internal", 2222, "deploy",
                                     options={"proxyjump": "jump@bastion", "serveraliveinterval": "30"},
                                     metadata={"tags": ["web"], "notes": "front"}))
        store.add_host(NewHost(dbs.id, "db-01", "db.internal", 5432, "postgres"))
        store.add_host(NewHost(prod.id, "files", "files.internal", 22, "u", protocol="sftp"))
        group_colors, host_colors = {"Prod": "#aa0000"}, {"Prod::Databases::db-01": "#123456"}
        doc, _report = build_export(store, group_colors=group_colors, host_colors=host_colors,
                                    credential_for=lambda _id: None, now=0, app_version="crossversion")
        favorite = f"host:{web.id}"
    finally:
        store.close()
    plain = F.dump_document(doc)
    paths["export"].write_bytes(plain)
    paths["encrypted"].write_bytes(F.encrypt_document(plain, PASSPHRASE))

    settings = AppSettings.load(paths["config"] / "settings.json")
    settings.connect_timeout = 12.5
    settings.host_key_policy = "reject"
    settings.save()
    ui = UiState()
    ui.window_size = (1200, 800)
    ui.group_colors = dict(group_colors)
    ui.host_colors = dict(host_colors)
    ui.recent_inventories = [str(paths["db"])]
    if hasattr(ui, "favorites"):
        ui.favorites = {str(paths["db"].resolve()): [favorite]}
    save_ui_state(paths["config"] / ".ui_state.json", ui.to_dict())
    save_smart_groups([SmartGroup("Production", "tag:prod")], paths["config"] / "smart_groups.json")

    keybox = Keybox.create(MASTER, paths["config"] / "keybox.json", scrypt_n=2 ** 15)
    secrets = SecureStore.for_keybox(keybox, paths["config"] / "passwords.json")
    for (host, port, user), secret in SECRETS.items():
        secrets.set(host, port, user, secret)
    macros = MacroStore(paths["config"] / "macros.enc")
    macros.unlock_with_key(keybox.subkey("macros"), keybox.id)
    macros.add(Macro(name="Uptime", shortcut=["<Primary><Alt>F8"], steps=[{"type": "text", "text": "uptime"}]))
    macros.save()


def read(folder: Path) -> dict:
    import dataclasses

    from ophion.gtkui.controllers.ui_state import UiState, load_ui_state
    from ophion.importers.native import read_native
    from ophion.inventory.manager import StoreConfigManager
    from ophion.inventory.store import InventoryStore
    from ophion.keybox import Keybox
    from ophion.macros import MacroStore
    from ophion.secure_store import SecureStore
    from ophion.settings import AppSettings
    from ophion.smart_groups import load_smart_groups

    paths = _paths(folder)
    store = InventoryStore(paths["db"])
    try:
        manager = StoreConfigManager(store)
        ids = {}
        hosts = []
        for parent, children in sorted(manager.tree.items()):
            for child, group in sorted(children.items()):
                for host in sorted(group.hosts, key=lambda h: h.alias):
                    ids[host.id] = f"<{parent}/{child}/{host.alias}>"
                    hosts.append([parent, child, host.alias, getattr(host, "protocol", "ssh"), dict(host.options),
                                  host.metadata])
        groups = sorted([[parent] for parent in manager.tree]
                        + [[parent, child] for parent, children in manager.tree.items() for child in children])
        version = store.schema_version
    finally:
        store.close()

    settings = AppSettings.load(paths["config"] / "settings.json")
    ui = UiState.from_dict(load_ui_state(paths["config"] / ".ui_state.json")).to_dict()
    favorites = ui.get("favorites", {})
    ui["favorites"] = {"<db>" if key == str(paths["db"].resolve()) else key:
                       [ids.get(item.partition(":")[2], item) for item in items] for key, items in favorites.items()}
    ui["recent_inventories"] = ["<db>" if item == str(paths["db"]) else item for item in ui["recent_inventories"]]
    for key in ("inventory_colors",):
        if key in ui:
            ui[key] = {"<db>" if scope == str(paths["db"].resolve()) else scope: value for scope, value in ui[key].items()}

    keybox = Keybox.load(paths["config"] / "keybox.json")
    unlocked = keybox.unlock(MASTER)
    secrets = SecureStore.for_keybox(keybox, paths["config"] / "passwords.json")
    macros = MacroStore(paths["config"] / "macros.enc")
    macros.unlock_with_key(keybox.subkey("macros"), keybox.id)

    def document(path, passphrase=None):
        found = dataclasses.asdict(read_native(path.read_bytes(), passphrase))
        for volatile in ("encrypted", "size"):
            found.pop(volatile, None)
        return json.loads(json.dumps(found, default=str))

    return {
        "inventory": {"schema_version": version, "groups": groups, "hosts": hosts},
        "settings": {"connect_timeout": settings.connect_timeout, "host_key_policy": settings.host_key_policy},
        "ui_state": ui,
        "smart_groups": [[g.name, g.query] for g in load_smart_groups(paths["config"] / "smart_groups.json")],
        "keybox": {"unlocked": unlocked,
                   "secrets": {f"{h}:{p}:{u}": secrets.get(h, p, u) == value for (h, p, u), value in SECRETS.items()},
                   "macros": [[m.name, m.shortcut, m.steps] for m in macros.list_sorted()]},
        "export": document(paths["export"]),
        "export_encrypted_matches": document(paths["encrypted"], PASSPHRASE) == document(paths["export"]),
    }


def main(argv) -> int:
    command, folder = argv[0], Path(argv[1]).resolve()
    _isolate(folder)
    if command == "create":
        create(folder)
    elif command == "read":
        sys.stdout.write(json.dumps(read(folder), indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    else:
        raise SystemExit(f"unknown command {command}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
