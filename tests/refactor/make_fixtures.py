"""Writes the persistence fixtures of tests/refactor/fixtures (run once, on the unmodified baseline).

    python3 -m tests.refactor.make_fixtures

They are *inputs* for ``test_persistence_golden.py`` and the cross-version harness: files as the pre-refactor
build writes (or users have) them. The keybox uses the minimum scrypt cost so the tests stay fast; its master
password and the export passphrase are below.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures")
MASTER_PASSWORD = "golden-master-password"
EXPORT_PASSPHRASE = "golden-export-passphrase"
PASSWORDS = {("web.internal", 22, "deploy"): "pw-web-1", ("db.internal", 5432, "postgres"): "pw-db-2",
             ("credential:cred-ops", 0, ""): "pw-credential-3"}

SETTINGS = {
    "host_key_policy": "reject", "connect_timeout": 15.5, "auth_timeout": 9, "banner_timeout": 10.0,
    "use_paramiko_vte": False, "passwords_off": False, "allowed_ciphers": ["aes256-gcm@openssh.com"],
    "allowed_kex": [], "secrets_policy": {"forbid_plaintext": True}, "enable_audit_logging": True,
    "audit_log_path": "/var/tmp/ophion-audit.log", "git_watch_enabled": True, "git_watch_path": "/srv/inventory",
    "git_watch_interval": 300,
    "terminal_appearance": {"font_family": "Monospace", "font_size": 12, "scheme": "solarized-dark"},
    "terminal_input": {"right_click": "menu", "middle_click": "paste-primary", "copy_on_select_clipboard": True},
    "local_shell": {"command": "/bin/bash", "login_shell": True, "start_in": "home", "on_exit": "hold"},
    "shortcuts": {"copy": ["<Primary><Shift>c"], "toggle-sidebar": []},
    "master_password_hash": "legacy-hash-ignored-once-keybox-exists",
    "some_future_key": {"kept": "or dropped, as the baseline does"},
}

UI_STATE = {
    "window_size": [1400, 900], "is_maximized": False, "sidebar_width": 320, "sidebar_visible": True,
    "theme_mode": "dark", "accent_color": "#3584e4", "density": "compact", "tab_label_mode": "name_ip",
    "font_scale": 1.1, "ui_font_size": 12, "filter_query": "tag:prod", "latency_probe": False,
    "group_colors": {"Prod": "#aa0000", "Prod/Databases": "#00aa00"},
    "host_colors": {"Prod::Databases::db-01": "#123456"},
    "recent_inventories": ["/home/u/.local/share/ophion/inventory.db", 7, ""],
    "favorites": {"/home/u/.local/share/ophion/inventory.db": ["host:abc", "group:def", "host:abc", 3]},
    "favorites_expanded": False,
    "page_tab": "details",
    "unknown_future_key": [1, 2, 3],
}

SMART_GROUPS = [{"name": " Production ", "query": "tag:prod"}, {"name": "", "query": "x:y"},
                {"name": "Web", "query": " proto:ssh AND tag:web "}, "garbage", {"name": "No query"}]


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def make() -> None:
    from ophion import native_format as F
    from ophion.exporters.native import build_export
    from ophion.keybox import Keybox
    from ophion.macros import Macro, MacroStore
    from ophion.secure_store import SecureStore
    from tests.native_fixture import build_rich, make_store

    FIXTURES.mkdir(exist_ok=True)
    write_json(FIXTURES / "settings.json", SETTINGS)
    write_json(FIXTURES / "ui_state.json", UI_STATE)
    write_json(FIXTURES / "smart_groups.json", SMART_GROUPS)

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(Path(tmp))
        try:
            _ids, group_colors, host_colors, credentials, _ = build_rich(store)
            doc, _report = build_export(store, group_colors=group_colors, host_colors=host_colors,
                                        credential_for=credentials.get, now=0, app_version="fixture")
        finally:
            store.close()
    plain = F.dump_document(doc)
    (FIXTURES / "rich.ophion").write_bytes(plain)
    (FIXTURES / "rich-encrypted.ophion").write_bytes(F.encrypt_document(plain, EXPORT_PASSPHRASE))

    keys = FIXTURES / "keybox"
    shutil.rmtree(keys, ignore_errors=True)
    keys.mkdir()
    keybox = Keybox.create(MASTER_PASSWORD, keys / "keybox.json", scrypt_n=2 ** 15)
    secrets = SecureStore.for_keybox(keybox, keys / "passwords.json")
    for (host, port, user), secret in PASSWORDS.items():
        secrets.set(host, port, user, secret)
    macros = MacroStore(keys / "macros.enc")
    macros.unlock_with_key(keybox.subkey("macros"), keybox.id)
    macros.add(Macro(name="Restart nginx", shortcut=["<Primary><Alt>F7"],
                     steps=[{"type": "text", "text": "sudo systemctl restart nginx"}, {"type": "key", "key": "Enter"},
                            {"type": "delay", "ms": 300}, {"type": "text", "text": "macro-secret", "secret": True}]))
    macros.add(Macro(name="Uptime", shortcut=[], steps=[{"type": "text", "text": "uptime"}]))
    macros.save()
    for leftover in keys.glob("*.bak"):
        leftover.unlink()


if __name__ == "__main__":
    sys.exit(make())
